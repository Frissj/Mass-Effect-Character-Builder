"""Read cooked Legendary Edition references and transfer stock face morphs."""
import ctypes
from dataclasses import dataclass
from functools import lru_cache
import json
from mmap import ACCESS_READ, mmap
import re
import struct

from mele_shader_cache import ShaderCache, static_parameter_set

PACKAGE_TAG = 0x9E2A83C1
VERSIONS = {(684, 171), (684, 168), (685, 205)}
EMPTY_ARRAY = b"\0" * 4


class OodleDecompressor:
    """Oodle block decompression through the game's own oo2core DLL."""

    def __init__(self, library):
        self.library = ctypes.CDLL(str(library))

    def decompress(self, payload, size, output_size):
        output = ctypes.create_string_buffer(output_size)
        written = self.library.OodleLZ_Decompress(
            ctypes.c_char_p(bytes(payload)), size, output, output_size, 0, 0, 0, None, None, None, None, None,
            None, 3)
        if written != output_size:
            raise ValueError(f"Oodle decompression returned {written} of {output_size} bytes.")
        return output.raw


def reference(data):
    return struct.unpack_from("<i", data)[0] if data else 0


def vector(data):
    return struct.unpack_from("<3f", data)


def gltf_position(point):
    x, y, z = point
    return x * 0.01, z * 0.01, y * 0.01


class Package:
    """Read the import, export and property tables from a cooked package."""

    def __init__(self, path, dll):
        with path.open("rb") as file:
            self.data = mmap(file.fileno(), 0, access=ACCESS_READ)
        tag, version, licensee = struct.unpack_from("<IHH", self.data)
        if tag != PACKAGE_TAG or (version, licensee) not in VERSIONS:
            raise ValueError(f"Unsupported Legendary Edition package format in {path.name}.")
        self.version = version
        folder_length, = struct.unpack_from("<i", self.data, 12)
        summary = 16 + (folder_length if folder_length >= 0 else -2 * folder_length)
        cooked = struct.unpack_from("<I", self.data, summary)[0] & 8
        start = summary + (8 if version == 685 and cooked else 4)
        self.name_count, self.name_offset, exports, export_offset, imports, import_offset = struct.unpack_from(
            "<6I", self.data, start)
        generation_count, = struct.unpack_from("<I", self.data, start + 60)
        compression = start + 80 + 12 * generation_count
        flags, count = struct.unpack_from("<II", self.data, compression)
        if flags not in (0, 0x400):
            raise ValueError(f"Unsupported face morph compression in {path.name}.")
        table = compression + 8
        self.chunks = [struct.unpack_from("<4I", self.data, table + i * 16) for i in range(count)]
        self.oodle = OodleDecompressor(str(dll)) if count else None
        self.cache = {}
        names = []
        pos = self.name_offset
        for _ in range(self.name_count):
            count, = struct.unpack("<i", self.read(pos, 4))
            size = -2 * count if count < 0 else count
            text = self.read(pos + 4, size)
            names.append(text.decode("utf-16-le" if count < 0 else "latin-1").rstrip("\0"))
            pos += 4 + size
        self.names = names
        self.entries = {}
        for i in range(imports):
            row = self.read(import_offset + i * 28, 28)
            self.entries[-i - 1] = Entry(self.fname(row, 20), reference(row[16:]), self.name(row, 8))
        pos = export_offset
        classes = {}
        for i in range(1, exports + 1):
            row = self.read(pos, 48)
            outer = reference(row[8:])
            size, offset = struct.unpack_from("<II", row, 32)
            self.entries[i] = Entry(self.fname(row, 12), outer, "Class", offset, size)
            classes[i] = reference(row)
            pos += 68 + 4 * reference(row[44:])
        self.entries.update({i: Entry(e.name, e.outer, self.entries[classes[i]].name if classes[i] else "Class",
                                     e.offset, e.size)
                             for i, e in self.entries.items() if i > 0})
        self.by_name = {}
        for i, entry in self.entries.items():
            self.by_name.setdefault(entry.name.casefold(), []).append(i)

    def read(self, offset, size):
        if not self.chunks:
            return self.data[offset:offset + size]
        for start, length, disk, packed_length in self.chunks:
            if start <= offset < start + length:
                if start not in self.cache:
                    tag, block_size, _, unpacked = struct.unpack_from("<4I", self.data, disk)
                    if tag != PACKAGE_TAG or unpacked != length or not block_size:
                        raise ValueError("Invalid face morph compression block.")
                    blocks = (length + block_size - 1) // block_size
                    pos = disk + 16 + 8 * blocks
                    result = bytearray()
                    for i in range(blocks):
                        packed, raw = struct.unpack_from("<II", self.data, disk + 16 + i * 8)
                        block = self.data[pos:pos + packed]
                        result.extend(block if packed == raw else self.oodle.decompress(block, packed, raw))
                        pos += packed
                    if len(result) != length or pos != disk + packed_length:
                        raise ValueError("Invalid face morph compression block size.")
                    self.cache[start] = result
                count = min(size, start + length - offset)
                data = self.cache[start][offset - start:offset - start + count]
                return data + self.read(offset + count, size - count) if count < size else data
        if size and offset + size <= self.chunks[0][0]:
            return self.data[offset:offset + size]
        if not size:
            return b""
        raise ValueError("Face morph data is outside the package's readable chunks.")

    def name(self, data, offset=0):
        index, = struct.unpack_from("<I", data, offset)
        return self.names[index]

    def fname(self, data, offset=0):
        name = self.name(data, offset)
        number, = struct.unpack_from("<I", data, offset + 4)
        return f"{name}_{number - 1}" if number else name

    def object_path(self, index):
        entry = self.entries[index]
        return (*self.object_path(entry.outer), entry.name) if entry.outer else (entry.name,)

    def props(self, index):
        entry = self.entries[index]
        data = self.read(entry.offset, entry.size)
        props, end = self.properties(data, 4)
        return props, data[end:]

    def properties(self, data, offset):
        props = {}
        while self.name(data, offset) != "None":
            name = self.name(data, offset)
            kind = self.name(data, offset + 8)
            size, = struct.unpack_from("<I", data, offset + 16)
            offset += 24 + {"StructProperty": 8, "ByteProperty": 8, "BoolProperty": 1}.get(kind, 0)
            props[name] = data[offset - 1:offset] if kind == "BoolProperty" else data[offset:offset + size]
            offset += size
        return props, offset + 8

    def structs(self, data):
        offset = 4
        for _ in range(struct.unpack_from("<I", data)[0]):
            props, offset = self.properties(data, offset)
            yield props


@dataclass(frozen=True)
class Entry:
    name: str
    outer: int
    kind: str
    offset: int = 0
    size: int = 0


def engine_config(root):
    """BioEngine.ini from the game's coalesced config: {(section, key): [values]}, casefolded.

    LE1 and LE2 store the coalesced inis as a table of UE strings
    (Coalesced_INT.bin). LE3 uses the 'mrmf' container (Coalesced.bin): a
    string table, a Huffman tree and a file/section/key index into
    Huffman-coded values.
    """
    config = {}
    legacy, packed = root / "CookedPCConsole/Coalesced_INT.bin", root / "CookedPCConsole/Coalesced.bin"
    if legacy.is_file():
        data, pos = legacy.read_bytes(), 0

        def integer():
            nonlocal pos
            value, = struct.unpack_from("<i", data, pos)
            pos += 4
            return value

        def string():
            nonlocal pos
            count = integer()
            size = -2 * count if count < 0 else count
            text = data[pos:pos + size].decode("utf-16-le" if count < 0 else "latin-1")
            pos += size
            return text.rstrip("\0")

        for _ in range(integer()):
            file = string().casefold()
            for _ in range(integer()):
                section = string().casefold()
                for _ in range(integer()):
                    key, value = string().casefold(), string()
                    if file.endswith("bioengine.ini"):
                        config.setdefault((section, key), []).append(value)
        return config
    if not packed.is_file():
        return config
    data = packed.read_bytes()
    magic, _, _, _, table_size, tree_size, index_size, _ = struct.unpack_from("<8I", data)
    if magic != 0x666D726D:
        raise ValueError("Unsupported Coalesced.bin container.")
    table, tree = 32, 32 + table_size
    index, values = tree + tree_size, tree + tree_size + index_size
    count, = struct.unpack_from("<I", data, table + 4)
    strings = []
    for i in range(count):
        position = table + 8 + struct.unpack_from("<I", data, table + 12 + 8 * i)[0]
        length, = struct.unpack_from("<H", data, position)
        strings.append(data[position + 2:position + 2 + length].decode("latin-1"))
    pairs = [struct.unpack_from("<ii", data, tree + 2 + 8 * i)
             for i in range(struct.unpack_from("<H", data, tree)[0])]
    bits = data[values + 4:]

    def decode(offset):
        text, node = [], len(pairs) - 1
        while True:
            bit = bits[offset >> 3] >> (offset & 7) & 1
            offset += 1
            child = pairs[node][bit]
            if child >= 0:
                node = child
                continue
            if child == -1:
                return "".join(text)
            text.append(chr(-1 - child))
            node = len(pairs) - 1

    def block(position):
        rows = struct.unpack_from("<H", data, position)[0]
        return [(strings[struct.unpack_from("<H", data, position + 2 + 6 * i)[0]].casefold(),
                 position + struct.unpack_from("<I", data, position + 4 + 6 * i)[0]) for i in range(rows)]

    for file, file_at in block(index):
        if not file.endswith("bioengine.ini"):
            continue
        for section, section_at in block(file_at):
            for key, key_at in block(section_at):
                items = struct.unpack_from("<H", data, key_at)[0]
                for i in range(items):
                    item, = struct.unpack_from("<I", data, key_at + 2 + 4 * i)
                    config.setdefault((section, key), []).append(decode(item & 0x1FFFFFFF))
    return config


def startup_packages(config):
    """Packages the engine has in memory from startup: startup and script packages."""
    wanted = (("engine.startuppackages", "package"), ("engine.scriptpackages", "nativepackages"),
              ("engine.scriptpackages", "nonnativepackages"), ("unrealed.editorengine", "editpackages"))
    return tuple(dict.fromkeys(value for key in wanted for value in config.get(key, ())))


@dataclass(frozen=True)
class MaterialRecord:
    master: str | None
    resource: bytes | None      # FMaterialResource Id: the RefShaderCache shader map key
    textures: tuple             # the resource's referenced textures, indexed by uniform expressions
    scalars: dict               # instance overrides by casefolded parameter name
    vectors: dict
    texture_values: dict
    props: dict                 # the master Material's properties, when it is cooked
    static_owner: bool          # an instance with its own static permutation owns the shader map
    owner: tuple | None = None  # (package, binary) of the material or instance owning the shader map
    blend: str | None = None    # the master's BlendMode; None when the master is not cooked


def material_resource(package, binary):
    """FMaterialResource: compile errors, texture dependency lengths, Id, texture list."""
    count = struct.unpack_from("<i", binary)[0]
    pos = 4
    for _ in range(count):
        length, = struct.unpack_from("<i", binary, pos)
        pos += 4 + (length if length >= 0 else -2 * length)
    pos += 4 + 8 * struct.unpack_from("<i", binary, pos)[0] + 4
    resource = bytes(binary[pos:pos + 16])
    pos += 20
    count, = struct.unpack_from("<i", binary, pos)
    textures = tuple((package, i) for i, in struct.iter_unpack("<i", binary[pos + 4:pos + 4 + 4 * count]))
    return resource, textures


@dataclass(frozen=True)
class FaceMorph:
    name: str
    head: str
    hair: str | None
    vertices: tuple
    bones: dict
    values: dict
    textures: dict


class GamePackages:
    def __init__(self, root):
        self.root = root
        self.dll = root.parent / "Binaries/Win64/oo2core_8_win64.dll"
        self.open = lru_cache(maxsize=10)(lambda name: Package(root / name, self.dll))
        self.shaders = ShaderCache(root / "CookedPCConsole/RefShaderCache-PC-D3D-SM5.upk")
        self.config = engine_config(root)
        self.startup = startup_packages(self.config)
        self.files = {}
        for file in root.rglob("*.pcc"):
            self.files.setdefault(file.stem.casefold(), []).append(file.relative_to(root).as_posix())
        self.shared = tuple(sorted(
            (file for name, files in self.files.items()
             if "master_mtr" in name or "player" in name or (name.startswith("bioc_") and "material" in name)
             for file in files if file.startswith("CookedPCConsole/")),
            key=str.casefold,
        ))

    def resolve(self, package, index):
        if index > 0:
            return package, index
        if not index:
            return None
        source = self.open(package)
        path = tuple(name.casefold() for name in source.object_path(index))
        kind = source.entries[index].kind
        # Seek-free packages may import an object from a group they already
        # contain. Match qualified paths before looking in other packages.
        candidates = [package, *self.files.get(path[0], ())]
        level = package.rsplit("/", 1)[-1].casefold().split("_", 2)
        if kind == "SkeletalMesh" and len(level) > 1 and level[0] == "biod":
            candidates.extend(self.files.get(f"biop_{level[1]}", ()))
        if kind in ("Material", "MaterialInstanceConstant", "RvrEffectsMaterialUser", "Texture2D", "TextureCube"):
            candidates.extend(self.shared)
        if not self.files.get(path[0]):
            # The outer package was cooked into packages the engine already has in
            # memory: the configured startup and script packages, and the ones the
            # referencing package imports from by name.
            names = [entry.name for i, entry in source.entries.items()
                     if i < 0 and not entry.outer and entry.kind == "Package"]
            candidates.extend(file for name in (*self.startup, *names)
                              for file in self.files.get(name.casefold(), ()) if file.startswith("CookedPCConsole/"))
        moved = []
        for target in dict.fromkeys(candidates):
            pcc = self.open(target)
            matches = [i for i in pcc.by_name.get(path[-1], ()) if i > 0 and pcc.entries[i].kind == kind]
            group_matches = []
            for i in matches:
                actual = tuple(name.casefold() for name in pcc.object_path(i))
                if actual == path or (path[0] == target.rsplit("/", 1)[-1].casefold().removesuffix(".pcc")
                                      and actual == path[1:]):
                    return target, i
                if actual[0] == path[0] and len(actual) > len(path):
                    groups = iter(actual[1:-1])
                    if all(group in groups for group in path[1:-1]):
                        group_matches.append(i)
            if len(group_matches) == 1:
                return target, group_matches[0]
            if target == package and len(matches) == 1:
                # Seek-free cooking can move a uniquely named local export
                # into a different group while retaining the original import.
                return target, matches[0]
            if target in self.shared and len(matches) == 1 and kind == "Texture2D" and "master_mtr" in path[0]:
                moved.append((target, matches[0]))
        if len(moved) == 1:
            return moved[0]
        return None

    def mesh(self, package, mesh):
        pcc = self.open(package)
        path = tuple(mesh.casefold().split("."))
        matches = [i for i in pcc.by_name.get(path[-1], ())
                   if i > 0 and pcc.entries[i].kind == "SkeletalMesh"]
        if len(path) > 1:
            matches = [i for i in matches if tuple(name.casefold() for name in pcc.object_path(i)) == path]
        if len(matches) != 1:
            raise ValueError(f"Expected one skeletal mesh named {mesh} in {package}.")
        return matches[0]

    def mesh_materials(self, package, mesh):
        pcc = self.open(package)
        _, binary = pcc.props(self.mesh(package, mesh))
        count = reference(binary[28:])
        if not 0 <= count <= (len(binary) - 32) // 4:
            raise ValueError(f"Invalid material table for {mesh}.")
        return [i for i, in struct.iter_unpack("<i", binary[32:32 + count * 4]) if i]

    def mesh_asset(self, package, mesh):
        try:
            self.mesh(package, mesh)
        except ValueError:
            pcc = self.open(package)
            imports = [i for i in pcc.by_name.get(mesh.casefold(), ())
                       if i < 0 and pcc.entries[i].kind == "SkeletalMesh"]
            if len(imports) != 1 or not (resolved := self.resolve(package, imports[0])):
                raise
            source, index = resolved
            return source, ".".join(self.open(source).object_path(index))
        return package, mesh

    def read_morph(self, package, mesh, character=None, companions=(), *, morph_name=None):
        pcc = self.open(package)
        candidates, via_hair = [], []
        for i, entry in pcc.entries.items():
            if i <= 0 or entry.kind != "BioMorphFace" or entry.name.startswith("Default__"):
                continue
            if morph_name and entry.name.casefold() != morph_name.casefold():
                continue
            props, binary = pcc.props(i)
            head_id = reference(props.get("m_oBaseHead"))
            hair_id = reference(props.get("m_oHairMesh"))
            if not head_id:
                continue
            head = pcc.entries[head_id].name
            hair = pcc.entries[hair_id].name if hair_id else None
            candidate = (entry.name, head, hair, props, binary)
            if head.casefold() == mesh.casefold():
                candidates.append(candidate)
            elif hair in companions and hair != mesh and "_hed_" in mesh.casefold():
                via_hair.append(candidate)
        candidates = candidates or via_hair
        if len(candidates) > 1:
            candidates = [row for row in candidates if row[2] in companions] or candidates
        if len(candidates) > 1 and character:
            words = [word for word in re.findall(r"[a-z0-9]+", character.casefold()) if len(word) > 2]
            candidates = [row for row in candidates if any(word in row[0].casefold() for word in words)]
        if len(candidates) != 1:
            return None
        name, head, hair, props, binary = candidates[0]
        if len(binary) < 12:
            return None
        lod_count, element_size, count = struct.unpack_from("<III", binary)
        if not lod_count or element_size != 12 or 12 + count * 12 > len(binary):
            raise ValueError(f"Unsupported LOD0 data for stock face morph {name}.")
        vertices = tuple(struct.iter_unpack("<3f", binary[12:12 + count * 12]))
        bones = {pcc.name(row["nName"]): gltf_position(vector(row["vPos"]))
                 for row in pcc.structs(props.get("m_aFinalSkeleton", EMPTY_ARRAY))}
        material = self.resolve(package, reference(props.get("m_oMaterialOverrides")))
        values, textures = self.morph_parameters(*material) if material else ({}, {})
        return FaceMorph(name, head, hair, vertices, bones, values, textures)

    def morph_parameters(self, package, index):
        pcc = self.open(package)
        props, _ = pcc.props(index)
        values = {pcc.name(row["nName"]): vector(row["cValue"])
                  for row in pcc.structs(props.get("m_aColorOverrides", EMPTY_ARRAY))}
        values.update({pcc.name(row["nName"]): struct.unpack("<f", row["sValue"])[0]
                       for row in pcc.structs(props.get("m_aScalarOverrides", EMPTY_ARRAY))})
        textures = {pcc.name(row["nName"]): (package, reference(row["m_pTexture"]))
                    for row in pcc.structs(props.get("m_aTextureOverrides", EMPTY_ARRAY))}
        return values, textures

    def material(self, package, index):
        """Follow a material reference to the compiled shader map that renders it.

        Instance parameters are collected leaf first. The shader map belongs to
        the nearest instance with its own static permutation, otherwise to the
        master material. Its FMaterialResource holds the shader map key and the
        texture list that uniform texture expressions index.
        """
        chain, owner, master, master_props, static_owner, blend = [], None, None, {}, False, None
        visited = set()
        ref = self.resolve(package, index)
        unresolved = None if ref else (package, index)
        while ref and ref not in visited:
            visited.add(ref)
            source, i = ref
            pcc = self.open(source)
            kind = pcc.entries[i].kind
            props, binary = pcc.props(i)
            if kind == "Material":
                master, master_props = pcc.entries[i].name, props
                blend = pcc.name(props["BlendMode"]) if "BlendMode" in props else "BLEND_Opaque"
                owner = owner or (source, binary)
                break
            if kind == "MaterialInstanceConstant":
                chain.append((source, props))
                if owner is None and props.get("bHasStaticPermutationResource", b"\0") != b"\0":
                    owner, static_owner = (source, binary), True
            # BioWare's effects wrapper renders its base material.
            parent = props.get("m_pBaseMaterial") if kind == "RvrEffectsMaterialUser" else props.get("Parent")
            parent_index = reference(parent)
            ref = self.resolve(source, parent_index)
            if ref is None and parent_index:
                unresolved = source, parent_index
        if master is None and unresolved:
            entry = self.open(unresolved[0]).entries[unresolved[1]]
            master = entry.name.removesuffix("_USER").removesuffix("_User").removesuffix("_user")
        resource, textures = material_resource(*owner) if owner else (None, ())
        scalars, vectors, texture_values = {}, {}, {}
        for source, instance in reversed(chain):
            pcc = self.open(source)
            for row in pcc.structs(instance.get("ScalarParameterValues", EMPTY_ARRAY)):
                scalars[pcc.name(row["ParameterName"]).casefold()] = struct.unpack("<f", row["ParameterValue"])[0]
            for row in pcc.structs(instance.get("VectorParameterValues", EMPTY_ARRAY)):
                vectors[pcc.name(row["ParameterName"]).casefold()] = struct.unpack("<4f", row["ParameterValue"])
            for row in pcc.structs(instance.get("TextureParameterValues", EMPTY_ARRAY)):
                if texture := reference(row.get("ParameterValue")):
                    texture_values[pcc.name(row["ParameterName"]).casefold()] = source, texture
        return MaterialRecord(master, resource, textures, scalars, vectors, texture_values, master_props,
                              static_owner, owner, blend)

    def shader_map(self, record):
        """The compiled shader map for a material, by resource key or by unique master name."""
        if record.resource and (found := self.shaders.shader_map(record.resource)):
            return found
        self.shaders.load()
        permutations = [self.shaders.shader_map(key)
                        for key in self.shaders.by_name.get((record.master or "").casefold(), [])]
        if record.static_owner:
            # The engine finds an instance's permutation by its static parameter set.
            package, binary = record.owner
            pcc = self.open(package)
            matches = []
            for candidate in permutations:
                start = binary.find(candidate.static[0], 20)
                while start >= 0:
                    try:
                        static, _ = static_parameter_set(binary, start, lambda at: pcc.name(binary, at))
                    except (struct.error, IndexError):
                        static = None
                    if static == candidate.static:
                        matches.append(candidate)
                        break
                    start = binary.find(candidate.static[0], start + 1)
            # Separately cooked copies of one permutation compile identical shaders.
            distinct = {(tuple(sorted((vf, tuple(sorted(rows.items()))) for vf, rows in m.mesh.items())),
                         m.vectors, m.scalars, m.textures, m.cubes): m for m in matches}
            return next(iter(distinct.values())) if len(distinct) == 1 else None
        # Without an instance permutation the master's default permutation renders:
        # its static parameter set is empty and keyed by the master's own resource.
        defaults = [found for found in permutations if found.default_permutation]
        return defaults[0] if len(defaults) == 1 else None


def psk_chunks(data):
    pos = 0
    chunks = {}
    while pos < len(data):
        name, _, stride, count = struct.unpack_from("<20sIII", data, pos)
        start = pos + 32
        pos = start + stride * count
        if pos > len(data):
            raise ValueError("Truncated face morph PSK chunk.")
        chunks[name.rstrip(b"\0")] = (stride, count, data[start:pos])
    return chunks


def transfer_positions(gltf, psk, positions):
    """PSK wedges retain original indices. Each glTF section reorders them."""
    chunks = psk_chunks(psk.read_bytes())
    stride, count, raw = chunks[b"VTXW0000"]
    if count != len(positions) or stride != 16:
        raise ValueError("Face morph vertex count does not match the exported head.")
    wedges = [struct.unpack_from("<I", raw, i * stride)[0] for i in range(count)]
    points = list(struct.iter_unpack("<3f", chunks[b"PNTS0000"][2]))
    face_stride, _, raw = chunks[b"FACE0000"]
    if face_stride != 12:
        raise ValueError("Unsupported face morph triangle layout.")
    sections = {}
    for a, b, c, material, _, _ in struct.iter_unpack("<3HBBI", raw):
        # UE Viewer mirrors the PSK and reverses its winding.
        sections.setdefault(material, []).extend((b, a, c))

    doc = json.loads(gltf.read_text(encoding="utf-8"))
    buffer_path = gltf.with_name(doc["buffers"][0]["uri"])
    binary = bytearray(buffer_path.read_bytes())

    def accessor(index, fmt):
        item = doc["accessors"][index]
        view = doc["bufferViews"][item["bufferView"]]
        start = view.get("byteOffset", 0) + item.get("byteOffset", 0)
        end = start + item["count"] * struct.calcsize(fmt)
        if view["buffer"] != 0 or view.get("byteStride") or end > len(binary):
            raise ValueError("Unexpected face morph glTF buffer layout.")
        return item, start, list(struct.iter_unpack(fmt, binary[start:end]))

    for slot, primitive in enumerate(doc["meshes"][0]["primitives"]):
        _, _, indices = accessor(primitive["indices"], "<H")
        item, offset, base = accessor(primitive["attributes"]["POSITION"], "<3f")
        remap = {}
        for (local,), old in zip(indices, sections[slot], strict=True):
            if remap.setdefault(local, old) != old:
                raise ValueError("PSK and glTF face morph vertex orders disagree.")
        changed = []
        for i, point in enumerate(base):
            old = remap[i]
            x, y, z = points[wedges[old]]
            original = gltf_position((x, -y, z))
            if max(abs(a - b) for a, b in zip(point, original)) > 0.00001:
                raise ValueError("PSK and glTF base face positions disagree.")
            changed.append(gltf_position(positions[old]))
        for i, point in enumerate(changed):
            struct.pack_into("<3f", binary, offset + 12 * i, *point)
        item["min"] = [min(p) for p in zip(*changed)]
        item["max"] = [max(p) for p in zip(*changed)]
    buffer_path.write_bytes(binary)
    gltf.write_text(json.dumps(doc), encoding="utf-8")
