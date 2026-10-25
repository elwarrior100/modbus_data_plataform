"""Construção da árvore OPC UA e publicação dos valores com estado e timestamps."""
from datetime import datetime, timezone

from asyncua import Server, ua

from .config import DeviceConfig, GatewayConfig, SignalConfig
from .poller import DeviceReading, SignalReading
from .status import is_bad
from .units import UNECE_NAMESPACE, UNITS

NAMESPACE_URI = "urn:industrial-lake:ot"
VARIANT = {"analog": ua.VariantType.Double, "counter": ua.VariantType.UInt32, "state": ua.VariantType.UInt16,
           "clock": ua.VariantType.DateTime}
DEFAULT = {ua.VariantType.Double: 0.0, ua.VariantType.UInt32: 0, ua.VariantType.UInt16: 0,
           ua.VariantType.Boolean: False, ua.VariantType.DateTime: datetime(1970, 1, 1, tzinfo=timezone.utc)}


class AddressSpace:
    def __init__(self, server: Server, ns: int):
        self.server, self.ns = server, ns
        self.nodes: dict[str, ua.NodeId] = {}
        self._last: dict[str, tuple] = {}

    @classmethod
    async def build(cls, server: Server, config: GatewayConfig) -> "AddressSpace":
        ns = await server.register_namespace(NAMESPACE_URI)
        space = cls(server, ns)
        plant = await server.nodes.objects.add_object(ua.NodeId("Plant", ns), ua.QualifiedName("Plant", ns))
        for device in config.devices:
            await space._add_device(plant, device)
        return space

    async def _add_device(self, plant, device: DeviceConfig) -> None:
        obj = await plant.add_object(ua.NodeId(device.name, self.ns), ua.QualifiedName(device.name, self.ns))
        await self._add_variable(obj, device.name, "Connected", ua.VariantType.Boolean)
        for sig in device.signals:
            var = await self._add_variable(obj, device.name, sig.name, VARIANT[sig.kind])
            await self._add_properties(var, sig)
        if device.alarm_coil is not None:
            await self._add_variable(obj, device.name, "alarm", ua.VariantType.Boolean)

    async def _add_variable(self, obj, device: str, name: str, vtype: ua.VariantType):
        node_id = ua.NodeId(f"{device}.{name}", self.ns)
        var = await obj.add_variable(node_id, ua.QualifiedName(name, self.ns), ua.Variant(DEFAULT[vtype], vtype))
        self.nodes[f"{device}.{name}"] = node_id
        await self._write(f"{device}.{name}", SignalReading(None, ua.StatusCodes.BadWaitingForInitialData), vtype, None)
        return var

    async def _add_properties(self, var, sig: SignalConfig) -> None:
        if sig.eu is not None:
            await var.add_property(self.ns, "EURange", ua.Range(float(sig.eu[0]), float(sig.eu[1])),
                                   datatype=ua.NodeId(ua.ObjectIds.Range))
        if sig.unit and sig.unit in UNITS and sig.kind != "clock":
            unit = UNITS[sig.unit]
            info = ua.EUInformation(NamespaceUri=UNECE_NAMESPACE, UnitId=unit.unit_id,
                                    DisplayName=ua.LocalizedText(unit.display), Description=ua.LocalizedText(unit.display))
            await var.add_property(self.ns, "EngineeringUnits", info, datatype=ua.NodeId(ua.ObjectIds.EUInformation))
        if sig.enum:
            await var.add_property(self.ns, "EnumStrings", [ua.LocalizedText(e) for e in sig.enum],
                                   varianttype=ua.VariantType.LocalizedText)

    async def _write(self, key: str, reading: SignalReading, vtype: ua.VariantType, source_time) -> None:
        if is_bad(reading.status) or reading.value is None:
            value = None
        elif vtype is ua.VariantType.DateTime:
            value = ua.Variant(datetime.fromtimestamp(reading.value, tz=timezone.utc), vtype)
        else:
            value = ua.Variant(reading.value, vtype)
        now = datetime.now(timezone.utc)
        data = ua.DataValue(value, ua.StatusCode(reading.status), source_time or now, now)
        await self.server.write_attribute_value(self.nodes[key], data)

    async def publish(self, device: DeviceConfig, reading: DeviceReading) -> None:
        """Escreve só o que mudou (valor ou estado), para as subscrições com deadband funcionarem."""
        async def put(name: str, item: SignalReading, vtype: ua.VariantType) -> None:
            key = f"{device.name}.{name}"
            state = (item.value, item.status)
            if self._last.get(key) != state:
                self._last[key] = state
                await self._write(key, item, vtype, None if is_bad(item.status) else reading.source_time)

        await put("Connected", SignalReading(reading.connected, ua.StatusCodes.Good), ua.VariantType.Boolean)
        for sig in device.signals:
            if sig.name in reading.signals:
                await put(sig.name, reading.signals[sig.name], VARIANT[sig.kind])
        if reading.alarm is not None:
            await put("alarm", reading.alarm, ua.VariantType.Boolean)
