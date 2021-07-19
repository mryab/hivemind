"""
Originally taken from: https://github.com/mhchia/py-libp2p-daemon-bindings
Licence: MIT
Author: Kevin Mai-Husan Chia
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Dict, Iterable, Optional, Sequence, Tuple

from multiaddr import Multiaddr, protocols

from hivemind.p2p.p2p_daemon_bindings.datastructures import PeerID, PeerInfo, StreamInfo
from hivemind.p2p.p2p_daemon_bindings.utils import DispatchFailure, raise_if_failed, read_pbmsg_safe, write_pbmsg
from hivemind.proto import p2pd_pb2 as p2pd_pb
from hivemind.utils.logging import get_logger

StreamHandler = Callable[[StreamInfo, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]

SUPPORT_CONN_PROTOCOLS = (
    protocols.P_IP4,
    # protocols.P_IP6,
    protocols.P_UNIX,
)
SUPPORTED_PROTOS = (protocols.protocol_with_code(proto) for proto in SUPPORT_CONN_PROTOCOLS)
logger = get_logger(__name__)


def parse_conn_protocol(maddr: Multiaddr) -> int:
    proto_codes = set(proto.code for proto in maddr.protocols())
    proto_cand = proto_codes.intersection(SUPPORT_CONN_PROTOCOLS)
    if len(proto_cand) != 1:
        raise ValueError(
            f"connection protocol should be only one protocol out of {SUPPORTED_PROTOS}" f", maddr={maddr}"
        )
    return tuple(proto_cand)[0]


class DaemonConnector:
    DEFAULT_CONTROL_MADDR = "/unix/tmp/p2pd.sock"

    def __init__(self, control_maddr: Multiaddr = Multiaddr(DEFAULT_CONTROL_MADDR)) -> None:
        self.control_maddr = control_maddr
        self.proto_code = parse_conn_protocol(self.control_maddr)

    async def open_connection(self) -> (asyncio.StreamReader, asyncio.StreamWriter):
        if self.proto_code == protocols.P_UNIX:
            control_path = self.control_maddr.value_for_protocol(protocols.P_UNIX)
            return await asyncio.open_unix_connection(control_path)
        elif self.proto_code == protocols.P_IP4:
            host = self.control_maddr.value_for_protocol(protocols.P_IP4)
            port = int(self.control_maddr.value_for_protocol(protocols.P_TCP))
            return await asyncio.open_connection(host, port)
        else:
            raise ValueError(f"Protocol not supported: {protocols.protocol_with_code(self.proto_code)}")

    async def open_persistent_connection(self) -> (asyncio.StreamReader, asyncio.StreamWriter):
        """
        Open connection to daemon and upgrade it to a persistent one
        """
        reader, writer = await self.open_connection()
        req = p2pd_pb.Request(type=p2pd_pb.Request.PERSISTENT_CONN_UPGRADE)
        await write_pbmsg(writer, req)

        return reader, writer


TUnaryHandler = Callable[[bytes, PeerID], Awaitable[bytes]]
CallID = uuid.UUID


class ControlClient:
    DEFAULT_LISTEN_MADDR = "/unix/tmp/p2pclient.sock"

    def __init__(
        self, daemon_connector: DaemonConnector, listen_maddr: Multiaddr = Multiaddr(DEFAULT_LISTEN_MADDR)
    ) -> None:
        self.listen_maddr = listen_maddr
        self.daemon_connector = daemon_connector
        self.handlers: Dict[str, StreamHandler] = {}

        # persistent connection readers & writers
        self._pers_conn_open: bool = False
        self.unary_handlers: Dict[str, TUnaryHandler] = {}

        self._ensure_conn_lock = asyncio.Lock()
        self.pending_messages: asyncio.Queue[p2pd_pb.Request] = asyncio.Queue()
        self.pending_calls: Dict[CallID, asyncio.Future[bytes]] = {}

    @asynccontextmanager
    async def listen(self) -> AsyncIterator["ControlClient"]:
        proto_code = parse_conn_protocol(self.listen_maddr)
        if proto_code == protocols.P_UNIX:
            listen_path = self.listen_maddr.value_for_protocol(protocols.P_UNIX)
            server = await asyncio.start_unix_server(self._handler, path=listen_path)
        elif proto_code == protocols.P_IP4:
            host = self.listen_maddr.value_for_protocol(protocols.P_IP4)
            port = int(self.listen_maddr.value_for_protocol(protocols.P_TCP))
            server = await asyncio.start_server(self._handler, port=port, host=host)
        else:
            raise ValueError(f"Protocol not supported: {protocols.protocol_with_code(proto_code)}")

        async with server:
            yield self

    async def _read_from_persistent_conn(self, reader: asyncio.StreamReader):
        while True:
            resp: p2pd_pb.Response = p2pd_pb.Response()  # type: ignore
            await read_pbmsg_safe(reader, resp)

            if resp.HasField("callUnaryResponse"):
                call_id = uuid.UUID(bytes=resp.callUnaryResponse.callId)

                if call_id in self.pending_calls and resp.callUnaryResponse.HasField("result"):
                    self.pending_calls[call_id].set_result(resp.callUnaryResponse.result)
                elif call_id in self.pending_calls and resp.callUnaryResponse.HasField("error"):
                    remote_exc = P2PHandlerError(str(resp.callUnaryResponse.error))
                    self.pending_calls[call_id].set_exception(remote_exc)
                else:
                    logger.debug(f"received unexpected unary call")

            elif resp.HasField("requestHandling"):
                asyncio.create_task(self._handle_persistent_request(resp.requestHandling))
                pass

    async def _write_to_persistent_conn(self, writer: asyncio.StreamWriter):
        while True:
            msg = await self.pending_messages.get()
            await write_pbmsg(writer, msg)

    async def _handle_persistent_request(self, request):
        assert request.proto in self.unary_handlers

        try:
            remote_id = PeerID(request.peer)
            response_payload: bytes = await self.unary_handlers[request.proto](request.data, remote_id)
            response = p2pd_pb.CallUnaryResponse(callId=request.callId, result=response_payload)
        except Exception as e:
            response = p2pd_pb.CallUnaryResponse(callId=request.callId, error=repr(e))

        await self.pending_messages.put(
            p2pd_pb.Request(type=p2pd_pb.Request.SEND_RESPONSE_TO_REMOTE, sendResponseToRemote=response)
        )

    async def _handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        pb_stream_info = p2pd_pb.StreamInfo()  # type: ignore
        await read_pbmsg_safe(reader, pb_stream_info)
        stream_info = StreamInfo.from_protobuf(pb_stream_info)
        try:
            handler = self.handlers[stream_info.proto]
        except KeyError as e:
            # should never enter here... daemon should reject the stream for us.
            writer.close()
            raise DispatchFailure(e)
        await handler(stream_info, reader, writer)

    async def _ensure_persistent_conn(self):
        if not self._pers_conn_open:
            with self._ensure_conn_lock:
                if not self._pers_conn_open:
                    reader, writer = await self.daemon_connector.open_persistent_connection()
                    asyncio.create_task(self._read_from_persistent_conn(reader))
                    asyncio.create_task(self._write_to_persistent_conn(writer))
                    self._pers_conn_open = True

    async def add_unary_handler(self, proto: str, handler: TUnaryHandler):
        await self._ensure_persistent_conn()

        add_unary_handler_req = p2pd_pb.AddUnaryHandlerRequest(proto=proto)
        req = p2pd_pb.Request(
            type=p2pd_pb.Request.ADD_UNARY_HANDLER,
            addUnaryHandler=add_unary_handler_req,
        )
        await self.pending_messages.put(req)

        if self.unary_handlers.get(proto):
            raise ValueError(f"Handler for protocol {proto} already assigned")
        self.unary_handlers[proto] = handler

    async def call_unary_handler(self, peer_id: PeerID, proto: str, data: bytes) -> bytes:
        call_id = uuid.uuid4()
        call_unary_req = p2pd_pb.CallUnaryRequest(
            peer=peer_id.to_bytes(),
            proto=proto,
            data=data,
            callId=call_id.bytes,
        )
        req = p2pd_pb.Request(
            type=p2pd_pb.Request.CALL_UNARY,
            callUnary=call_unary_req,
        )

        await self._ensure_persistent_conn()

        try:
            self.pending_calls[call_id] = asyncio.Future()
            await self.pending_messages.put(req)
            return await self.pending_calls[call_id]
        finally:
            await self.pending_calls.pop(call_id)

    async def identify(self) -> Tuple[PeerID, Tuple[Multiaddr, ...]]:
        reader, writer = await self.daemon_connector.open_connection()
        req = p2pd_pb.Request(type=p2pd_pb.Request.IDENTIFY)
        await write_pbmsg(writer, req)

        resp = p2pd_pb.Response()  # type: ignore
        await read_pbmsg_safe(reader, resp)
        writer.close()

        raise_if_failed(resp)
        peer_id_bytes = resp.identify.id
        maddrs_bytes = resp.identify.addrs

        maddrs = tuple(Multiaddr(maddr_bytes) for maddr_bytes in maddrs_bytes)
        peer_id = PeerID(peer_id_bytes)

        return peer_id, maddrs

    async def connect(self, peer_id: PeerID, maddrs: Iterable[Multiaddr]) -> None:
        reader, writer = await self.daemon_connector.open_connection()

        maddrs_bytes = [i.to_bytes() for i in maddrs]
        connect_req = p2pd_pb.ConnectRequest(peer=peer_id.to_bytes(), addrs=maddrs_bytes)
        req = p2pd_pb.Request(type=p2pd_pb.Request.CONNECT, connect=connect_req)
        await write_pbmsg(writer, req)

        resp = p2pd_pb.Response()  # type: ignore
        await read_pbmsg_safe(reader, resp)
        writer.close()
        raise_if_failed(resp)

    async def list_peers(self) -> Tuple[PeerInfo, ...]:
        req = p2pd_pb.Request(type=p2pd_pb.Request.LIST_PEERS)
        reader, writer = await self.daemon_connector.open_connection()
        await write_pbmsg(writer, req)
        resp = p2pd_pb.Response()  # type: ignore
        await read_pbmsg_safe(reader, resp)
        writer.close()
        raise_if_failed(resp)

        peers = tuple(PeerInfo.from_protobuf(pinfo) for pinfo in resp.peers)
        return peers

    async def disconnect(self, peer_id: PeerID) -> None:
        disconnect_req = p2pd_pb.DisconnectRequest(peer=peer_id.to_bytes())
        req = p2pd_pb.Request(type=p2pd_pb.Request.DISCONNECT, disconnect=disconnect_req)
        reader, writer = await self.daemon_connector.open_connection()
        await write_pbmsg(writer, req)
        resp = p2pd_pb.Response()  # type: ignore
        await read_pbmsg_safe(reader, resp)
        writer.close()
        raise_if_failed(resp)

    async def stream_open(
        self, peer_id: PeerID, protocols: Sequence[str]
    ) -> Tuple[StreamInfo, asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await self.daemon_connector.open_connection()

        stream_open_req = p2pd_pb.StreamOpenRequest(peer=peer_id.to_bytes(), proto=list(protocols))
        req = p2pd_pb.Request(type=p2pd_pb.Request.STREAM_OPEN, streamOpen=stream_open_req)
        await write_pbmsg(writer, req)

        resp = p2pd_pb.Response()  # type: ignore
        await read_pbmsg_safe(reader, resp)
        raise_if_failed(resp)

        pb_stream_info = resp.streamInfo
        stream_info = StreamInfo.from_protobuf(pb_stream_info)

        return stream_info, reader, writer

    async def stream_handler(self, proto: str, handler_cb: StreamHandler) -> None:
        reader, writer = await self.daemon_connector.open_connection()

        listen_path_maddr_bytes = self.listen_maddr.to_bytes()
        stream_handler_req = p2pd_pb.StreamHandlerRequest(addr=listen_path_maddr_bytes, proto=[proto])
        req = p2pd_pb.Request(type=p2pd_pb.Request.STREAM_HANDLER, streamHandler=stream_handler_req)
        await write_pbmsg(writer, req)

        resp = p2pd_pb.Response()  # type: ignore
        await read_pbmsg_safe(reader, resp)
        writer.close()
        raise_if_failed(resp)

        # if success, add the handler to the dict
        self.handlers[proto] = handler_cb


class P2PHandlerError(Exception):
    """
    Raised if remote handled a request with an exception
    """
