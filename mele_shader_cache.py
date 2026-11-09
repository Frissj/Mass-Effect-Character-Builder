"""Read compiled material shaders and uniform expressions from a Legendary Edition RefShaderCache."""
from dataclasses import dataclass
from mmap import ACCESS_READ, mmap
import struct

# FMaterialUniformExpression classes and the number of child expressions and
# trailing bytes each one serializes after its class name.
NODES = {
    "Abs": (1, 0), "Ceil": (1, 0), "Floor": (1, 0), "Frac": (1, 0), "Periodic": (1, 0),
    "SquareRoot": (1, 0), "Sine": (1, 4), "Fmod": (2, 0), "Min": (2, 0), "Max": (2, 0),
    "AppendVector": (2, 4), "FoldedMath": (2, 1), "Clamp": (3, 0),
}
LEAVES = {"Time": 0, "RealTime": 0, "FractionOfEffectEnabled": 0}


def static_parameter_set(data, offset, fname):
    """FStaticParameterSet: base material Id, then switch, component mask and normal settings.

    Each parameter is (name, value..., bOverride, expression guid); a permutation
    is identified by the base Id and the values of the overridden parameters.
    """
    base = bytes(data[offset:offset + 16])
    offset += 16
    groups = []
    for width, fmt in ((32, "<I"), (44, "<4I"), (29, "<B")):
        count, = struct.unpack_from("<I", data, offset)
        offset += 4
        rows = []
        for _ in range(count):
            values = struct.unpack_from(fmt, data, offset + 8)
            override, = struct.unpack_from("<I", data, offset + 8 + struct.calcsize(fmt))
            if override:
                rows.append((fname(offset).casefold(), values))
            offset += width
        groups.append(tuple(sorted(rows)))
    return (base, *groups), offset


@dataclass(frozen=True)
class ShaderMap:
    name: str
    resource: bytes
    mesh: dict          # vertex factory -> {shader type: shader guid}
    material: dict      # shader type -> shader guid
    vectors: tuple      # pixel uniform vector expressions
    scalars: tuple      # pixel uniform scalar expressions
    textures: tuple     # pixel uniform 2D texture expressions
    cubes: tuple        # pixel uniform cube texture expressions
    default_permutation: bool  # no static switches, masks or normal settings: the master's own map
    static: tuple              # the static parameter set this permutation was compiled for


class ShaderCache:
    def __init__(self, path):
        self.path = path
        self.data = None

    # ------------------------------------------------------------ primitives

    def u32(self, offset):
        return struct.unpack_from("<I", self.data, offset)[0]

    def string(self, offset):
        count, = struct.unpack_from("<i", self.data, offset)
        end = offset + 4 + (count if count >= 0 else -2 * count)
        raw = self.data[offset + 4:end]
        return raw.decode("latin-1" if count >= 0 else "utf-16-le").rstrip("\0"), end

    def fname(self, offset):
        return self.names[self.u32(offset)]

    def skip_static_parameters(self, offset):
        offset += 16
        for width in (32, 44, 29):
            offset += 4 + self.u32(offset) * width
        return offset

    def static_parameters(self, offset):
        return static_parameter_set(self.data, offset, self.fname)

    # ------------------------------------------------------------ index

    def load(self):
        if self.data is not None:
            return
        with self.path.open("rb") as file:
            self.data = mmap(file.fileno(), 0, access=ACCESS_READ)
        _, summary = self.string(12)
        count, pos, _, export_offset = struct.unpack_from("<4I", self.data, summary + 4)
        names = []
        for _ in range(count):
            name, pos = self.string(pos)
            names.append(name)
        self.names = names
        pos = self.u32(export_offset + 36) + 13
        for _ in range(2):  # shader type and vertex factory CRC maps
            pos += 4 + 12 * self.u32(pos)
        count = self.u32(pos)
        pos += 4
        self.shaders = {}
        for _ in range(count):
            self.shaders[bytes(self.data[pos + 8:pos + 24])] = pos
            pos = self.u32(pos + 24)
        pos += 4 + 12 * self.u32(pos)
        count = self.u32(pos)
        pos += 4
        self.maps, self.by_name = {}, {}
        for _ in range(count):
            body = self.skip_static_parameters(pos)
            end = self.u32(body + 8)
            if end <= pos or end > len(self.data):
                raise ValueError("Invalid material shader map offset.")
            cursor = body + 12 + 4 + 32 * self.u32(body + 12)
            meshes = self.u32(cursor)
            cursor += 4
            for _ in range(meshes):
                cursor += 4 + 32 * self.u32(cursor) + 8
            resource = bytes(self.data[cursor:cursor + 16])
            self.maps[resource] = pos
            self.by_name.setdefault(self.string(cursor + 16)[0].casefold(), []).append(resource)
            pos = end

    # ------------------------------------------------------------ records

    def entries(self, offset):
        count = self.u32(offset)
        rows = {self.fname(offset + 4 + 32 * i): bytes(self.data[offset + 12 + 32 * i:offset + 28 + 32 * i])
                for i in range(count)}
        return rows, offset + 4 + 32 * count

    def expression(self, offset):
        kind = self.fname(offset).removeprefix("FMaterialUniformExpression")
        offset += 8
        if kind in ("VectorParameter", "ScalarParameter"):
            name = self.fname(offset)
            if kind == "VectorParameter":
                return ("VectorParameter", name, struct.unpack_from("<4f", self.data, offset + 8)), offset + 24
            return ("ScalarParameter", name, struct.unpack_from("<f", self.data, offset + 8)[0]), offset + 12
        if kind == "Constant":
            return ("Constant", struct.unpack_from("<4f", self.data, offset)), offset + 17
        if kind == "Texture":
            return ("Texture", struct.unpack_from("<i", self.data, offset)[0]), offset + 4
        if kind == "TextureParameter":
            # ParameterName, then the index into the owning resource's texture list.
            return ("TextureParameter", self.fname(offset), struct.unpack_from("<i", self.data, offset + 8)[0]), offset + 12
        if kind in NODES:
            children, extra = NODES[kind]
            args = []
            for _ in range(children):
                child, offset = self.expression(offset)
                args.append(child)
            tail = bytes(self.data[offset:offset + extra])
            return (kind, *args, tail), offset + extra
        if kind in LEAVES:
            return (kind,), offset + LEAVES[kind]
        raise ValueError(f"Unsupported shader uniform expression {kind}.")

    def expressions(self, offset):
        rows = []
        count = self.u32(offset)
        offset += 4
        for _ in range(count):
            row, offset = self.expression(offset)
            rows.append(row)
        return tuple(rows), offset

    def shader_map(self, resource):
        self.load()
        pos = self.maps.get(bytes(resource))
        if pos is None:
            return None
        body = self.skip_static_parameters(pos)
        default = self.data[pos:pos + 16] == bytes(resource) and body == pos + 28
        material, cursor = self.entries(body + 12)
        mesh = {}
        meshes = self.u32(cursor)
        cursor += 4
        for _ in range(meshes):
            rows, cursor = self.entries(cursor)
            mesh[self.fname(cursor)] = rows
            cursor += 8
        name, cursor = self.string(cursor + 16)
        cursor = self.skip_static_parameters(cursor)
        vectors, cursor = self.expressions(cursor)
        scalars, cursor = self.expressions(cursor)
        textures, cursor = self.expressions(cursor)
        cubes, cursor = self.expressions(cursor)
        static, _ = self.static_parameters(pos)
        return ShaderMap(name, bytes(resource), mesh, material, vectors, scalars, textures, cubes, default, static)

    def bytecode(self, guid):
        """Return the DXBC container of a compiled shader."""
        self.load()
        pos = self.shaders[bytes(guid)] + 28
        length = self.u32(pos + 2)
        code = bytes(self.data[pos + 6:pos + 6 + length])
        if code[:4] != b"DXBC":
            raise ValueError("Unexpected shader bytecode container.")
        return code
