"""O collector: subscreve o gateway OPC UA e põe cada alteração no spool, sem perder nem repetir."""
import asyncio
import json
import logging
from datetime import datetime, timezone

from asyncua import Client, ua
from asyncua.common.subscription import OverflowPolicy

from .envelope import Event, to_us
from .spool import Spool

log = logging.getLogger("ingest.collector")
NAMESPACE_URI = "urn:industrial-lake:ot"
SOURCE = "ot-gateway"


def encode_value(value) -> str | None:
    """O valor tal como veio, em JSON. Os instantes viajam como texto ISO 8601."""
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc).isoformat()
    return json.dumps(value)


def to_event(node_id: str, data_value: ua.DataValue, ingest_us: int, source: str = SOURCE) -> Event:
    status = data_value.StatusCode.value
    bad = data_value.StatusCode.is_bad()
    raw = None if bad or data_value.Value is None else encode_value(data_value.Value.Value)
    return Event.build(source, node_id, raw, status, to_us(data_value.SourceTimestamp),
                       to_us(data_value.ServerTimestamp), ingest_us)


class _Handler:
    """Chamado pelo asyncua a cada alteração. Só põe a amostra na fila do collector; quem a escreve é o flusher."""

    def __init__(self, queue: "_Queue", source: str, clock):
        self.queue, self.source, self.clock = queue, source, clock

    def datachange_notification(self, node, val, data):
        self.queue.put(to_event(node.nodeid.to_string(), data.monitored_item.Value, to_us(self.clock()), self.source))


class _Queue:
    """Amostras ainda não gravadas. Se o spool falhar (rede, reinício do Postgres), ficam aqui em memória
    e tenta-se outra vez; só saem da fila depois de gravadas."""

    def __init__(self, spool: Spool, retry_s: float = 1.0):
        self.spool, self.retry_s = spool, retry_s
        self._items: list[Event] = []
        self._wake = asyncio.Event()

    def put(self, event: Event) -> None:
        self._items.append(event)
        self._wake.set()

    @property
    def waiting(self) -> int:
        return len(self._items)

    async def flush_forever(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            batch = list(self._items)
            try:
                await asyncio.to_thread(self.spool.add, batch)          # fora do ciclo de eventos: pode bloquear na rede
                del self._items[:len(batch)]                             # só agora é que saem da fila
            except Exception as error:
                log.warning("spool indisponível (%s): %d amostras ficam em memória; nova tentativa em %.0fs",
                            type(error).__name__, len(self._items), self.retry_s)
                await asyncio.sleep(self.retry_s)
                self._wake.set()


class Collector:
    def __init__(self, url: str, spool: Spool, *, source: str = SOURCE, period_ms: float = 500,
                 heartbeat_s: float = 2.0, backoff_max_s: float = 30.0, clock=None):
        self.url, self.spool, self.source = url, spool, source
        self.period_ms, self.heartbeat_s, self.backoff_max_s = period_ms, heartbeat_s, backoff_max_s
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.connected = asyncio.Event()
        self.sessions = 0
        self.queue = _Queue(spool)

    async def run(self) -> None:
        """Liga-se, subscreve e vigia a ligação. Se cair, espera cada vez mais tempo (1 s, 2 s, 4 s...
        até ao máximo) e volta a tentar, para sempre."""
        flusher = asyncio.create_task(self.queue.flush_forever())
        delay = 1.0
        try:
            while True:
                try:
                    await self._session()
                    delay = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.warning("sessão OPC UA terminou (%s: %s); nova tentativa em %.0fs", type(error).__name__, error, delay)
                self.connected.clear()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.backoff_max_s)
        finally:
            flusher.cancel()

    async def _session(self) -> None:
        client = Client(self.url, timeout=5)
        async with client:
            ns = await client.get_namespace_index(NAMESPACE_URI)
            plant = await client.nodes.objects.get_child(f"{ns}:Plant")
            variables = []
            for asset in await plant.get_children():
                variables += await asset.get_children(nodeclassmask=ua.NodeClass.Variable)
            subscription = await client.create_subscription(
                self.period_ms, _Handler(self.queue, self.source, self.clock), queue_maxsize=100_000, overflow=OverflowPolicy.DISCONNECT)
            await subscription.subscribe_data_change(variables, queuesize=50, sampling_interval=0)
            self.sessions += 1
            self.connected.set()
            log.info("ligado a %s: %d variáveis subscritas", self.url, len(variables))
            state = client.get_node(ua.NodeId(ua.ObjectIds.Server_ServerStatus_State))
            while True:                      # batimento: se o servidor desaparecer, isto falha e reconectamos
                await asyncio.sleep(self.heartbeat_s)
                await state.read_value()
