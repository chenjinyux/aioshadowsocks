import asyncio
import logging
import socket
import struct

from shadowsocks import protocol_flag as flag
from shadowsocks.cipherman import CipherMan
from shadowsocks.metrics import ACTIVE_CONNECTION_COUNT, CONNECTION_MADE_COUNT
from shadowsocks.utils import parse_header


class LocalHandler:
    """
    事件循环一共处理五个状态

    STAGE_INIT  初始状态 socket5握手
    STAGE_CONNECT 连接建立阶段 从本地获取addr 进行dns解析
    STAGE_STREAM 建立管道(pipe) 进行socket5传输
    STAGE_DESTROY 结束连接状态
    STAGE_ERROR 异常状态
    """

    STAGE_INIT = 0
    STAGE_CONNECT = 1
    STAGE_STREAM = 2
    STAGE_DESTROY = -1
    STAGE_ERROR = 255

    def __init__(self, port):
        super().__init__()

        self.port = port
        self.cipher = None

        self._stage = None
        self._peername = None
        self._remote = None
        self._transport = None
        self._transport_protocol = None
        self._transport_protocol_human = None
        self._is_closing = False
        self._connect_buffer = bytearray()

    def _init_transport(self, transport: asyncio.Transport, peername, protocol):
        self._stage = self.STAGE_INIT
        self._transport = transport
        self._peername = peername
        self._transport_protocol = protocol
        if protocol == flag.TRANSPORT_TCP:
            self._transport_protocol_human = "tcp"
        else:
            self._transport_protocol_human = "udp"

    def _init_cipher(self):
        self.cipher = CipherMan.get_cipher_by_port(self.port, self._transport_protocol)

    def close(self):
        self._stage = self.STAGE_DESTROY
        if self._is_closing:
            return
        self._is_closing = True
        ACTIVE_CONNECTION_COUNT.inc(-1)

        if self._transport_protocol == flag.TRANSPORT_TCP:
            self._transport and self._transport.close()
            self.cipher and self.cipher.incr_user_tcp_num(-1)
        self._remote and self._remote.close()

    def write(self, data):
        if self._transport_protocol == flag.TRANSPORT_TCP:
            if self._transport.is_closing():
                return
            self._transport.write(data)
        else:
            self._transport.sendto(data, self._peername)

    def handle_connection_made(self, transport_protocol, transport, peername):
        self._init_transport(transport, peername, transport_protocol)
        self._init_cipher()

    def handle_eof_received(self):
        self.close()

    def handle_connection_lost(self, exc):
        self.close()

    def handle_data_received(self, data):

        try:
            data = self.cipher.decrypt(data)
        except Exception as e:
            self.close()
            logging.warning(
                f"decrypt data error:{e} remote:{self._peername},type:{self._transport_protocol_human} closing..."
            )
            return

        if not data:
            return

        if self._stage == self.STAGE_INIT:
            asyncio.create_task(self._handle_stage_init(data))
        elif self._stage == self.STAGE_CONNECT:
            self._handle_stage_connect(data)
        elif self._stage == self.STAGE_STREAM:
            self._handle_stage_stream(data)
        elif self._stage == self.STAGE_ERROR:
            self.close()
        elif self._stage == self.STAGE_DESTROY:
            self.close()
        else:
            logging.warning(f"unknown stage:{self._stage}")

    async def _handle_stage_init(self, data):
        addr_type, dst_addr, dst_port, header_length = parse_header(data)
        if not all([addr_type, dst_addr, dst_port, header_length]):
            logging.warning(f"parse error addr_type: {addr_type} port: {self.port}")
            self.close()
            return
        else:
            payload = data[header_length:]
        logging.debug(
            f"HEADER: {addr_type} - {dst_addr} - {dst_port} - {self._transport_protocol}"
        )

        loop = asyncio.get_running_loop()
        if self._transport_protocol == flag.TRANSPORT_TCP:
            self._stage = self.STAGE_CONNECT
            self._handle_stage_connect(payload)
            try:
                _, remote_tcp = await loop.create_connection(
                    lambda: RemoteTCP(self), dst_addr, dst_port
                )
            except Exception as e:
                self._stage = self.STAGE_ERROR
                self.close()
                logging.warning(f"connection failed, {type(e)} e: {e}")
            else:
                self._remote = remote_tcp
                self.cipher.record_user_ip(self._peername)
        else:
            try:
                await self.create_datagram_endpoint(
                    lambda: RemoteUDP(dst_addr, dst_port, payload, self),
                    remote_addr=(dst_addr, dst_port),
                )
            except Exception as e:
                self._stage = self.STAGE_ERROR
                self.close()
                logging.warning(f"connection failed, {type(e)} e: {e}")

    def _handle_stage_connect(self, data):
        # 在握手之后，会耗费一定时间来来和remote建立连接,但是ss-client并不会等这个时间
        if not self._remote or self._remote.ready == False:
            self._connect_buffer.extend(data)
        else:
            self._stage = self.STAGE_STREAM
            self._handle_stage_stream(data)

    def _handle_stage_stream(self, data):
        self._remote.write(data)
        logging.debug(f"relay data length {len(data)}")


class LocalTCP(asyncio.Protocol):
    """
    Local Tcp Factory
    """

    def __init__(self, port):
        self.port = port
        self._handler = None

    def _init_handler(self):
        self._handler = LocalHandler(self.port)

    def __call__(self):
        local = LocalTCP(self.port)
        local._init_handler()
        return local

    def pause_writing(self):
        self._handler._remote._transport.pause_reading()

    def resume_writing(self):
        self._handler._remote._transport.resume_reading()

    def connection_made(self, transport):
        self._transport = transport
        peername = self._transport.get_extra_info("peername")
        self._handler.handle_connection_made(flag.TRANSPORT_TCP, transport, peername)
        CONNECTION_MADE_COUNT.inc()
        ACTIVE_CONNECTION_COUNT.inc()

    def data_received(self, data):
        self._handler.handle_data_received(data)

    def eof_received(self):
        self._handler.handle_eof_received()

    def connection_lost(self, exc):
        self._handler.handle_connection_lost(exc)


class RemoteTCP(asyncio.Protocol):
    def __init__(self, local_handler):
        super().__init__()

        self.local = local_handler
        self.peername = None
        self._transport = None
        self.cipher = CipherMan(access_user=local_handler.cipher.access_user)
        self.ready = False

        self._is_closing = False

    def write(self, data):
        if not self._transport.is_closing():
            self._transport.write(data)

    def close(self):
        if self._is_closing:
            return
        self._is_closing = True
        ACTIVE_CONNECTION_COUNT.inc(-1)

        self._transport and self._transport.close()
        self.local.close()

    def connection_made(self, transport: asyncio.Transport):
        self._transport = transport
        self.peername = self._transport.get_extra_info("peername")
        transport.write(self.local._connect_buffer)
        self.ready = True
        CONNECTION_MADE_COUNT.inc()
        ACTIVE_CONNECTION_COUNT.inc()

    def data_received(self, data):
        self.local.write(self.cipher.encrypt(data))

    def pause_reading(self):
        self.local._transport.pause_reading()

    def resume_reading(self):
        self.local._transport.resume_reading()

    def eof_received(self):
        self.close()

    def connection_lost(self, exc):
        self.close()


class LocalUDP(asyncio.DatagramProtocol):
    """
    Local Udp Factory
    """

    def __init__(self, port):
        self.port = port
        self._protocols = {}
        self._transport = None

    def __call__(self):
        local = LocalUDP(self.port)
        return local

    def connection_made(self, transport):
        self._transport = transport

    def datagram_received(self, data, peername):
        if peername in self._protocols:
            handler = self._protocols[peername]
        else:
            handler = LocalHandler(self.port)
            self._protocols[peername] = handler
            handler.handle_connection_made(
                flag.TRANSPORT_UDP, self._transport, peername
            )
        handler.handle_data_received(data)

    def error_received(self, exc):
        # TODO clean udp conn
        pass


class RemoteUDP(asyncio.DatagramProtocol):
    def __init__(self, addr, port, data, local_hander):
        super().__init__()
        self.data = data
        self.local = local_hander
        self.peername = None
        self._transport = None
        self.cipher = CipherMan(
            access_user=self.local.cipher.access_user, ts_protocol=flag.TRANSPORT_UDP
        )
        self._is_closing = False

    def write(self, data):
        self._transport and not self._transport.is_closing() and self._transport.sendto(
            data
        )

    def close(self):
        if self._is_closing:
            return
        self._is_closing = True

        self._transport and self._transport.close()
        del self.local

    def connection_made(self, transport):
        self._transport = transport
        self.peername = self._transport.get_extra_info("peername")
        self.write(self.data)

    def datagram_received(self, data, peername, *arg):

        assert self.peername == peername
        # 源地址和端口
        bind_addr = peername[0]
        bind_port = peername[1]
        if "." in bind_addr:
            addr = socket.inet_pton(socket.AF_INET, bind_addr)
        elif ":" in bind_addr:
            addr = socket.inet_pton(socket.AF_INET6, bind_addr)
        else:
            raise Exception("add not valid")
        port = struct.pack("!H", bind_port)
        # 构造返回的报文结构
        data = b"\x01" + addr + port + data
        data = self.cipher.encrypt(data)
        self.local.write(data)

    def error_received(self, exc):
        self.close()

    def connection_lost(self, exc):
        self.close()
