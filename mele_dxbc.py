"""Execute compiled Shader Model 5 pixel shaders over a texel grid with numpy.

The game's materials only exist as compiled DXBC. This module disassembles a
shader with the system's D3DCompiler, then evaluates it per component: each
register component is a float32 array over the grid (or a uniform scalar).
"""
import ctypes
import re

import numpy as np

COMPONENTS = "xyzw"
F32 = np.float32


# ---------------------------------------------------------------- disassembly

def disassemble(code):
    compiler = ctypes.WinDLL("d3dcompiler_47.dll")
    blob = ctypes.c_void_p()
    if compiler.D3DDisassemble(code, len(code), 0, None, ctypes.byref(blob)):
        raise ValueError("D3DCompiler could not disassemble the shader.")
    table = ctypes.cast(ctypes.cast(blob, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    pointer = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(table[3])(blob)
    size = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(table[4])(blob)
    text = ctypes.string_at(pointer, size).decode("latin-1").replace("\0", "")
    ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(blob)
    return text


OPERAND = re.compile(
    r"(?P<neg>-)?(?P<abs>\|)?(?:"
    r"l\((?P<lit>[^)]*)\)"
    r"|(?P<reg>[rvo]|cb|icb|x)(?P<index>\d*)(?:\[(?P<sub>[^\]]+)\])?(?:\.(?P<swz>[xyzw]+))?"
    r"|(?P<res>[ts])(?P<slot>\d+)(?:\.(?P<rswz>[xyzw]+))?"
    r"|(?P<null>null))\|?"
)


def split_operands(text):
    parts, depth, current = [], 0, ""
    for char in text:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def literal_bits(text):
    values = []
    for item in text.split(","):
        item = item.strip()
        if item.lower().startswith(("0x", "-0x")):
            values.append(np.array(int(item, 16) & 0xFFFFFFFF, np.uint32).view(F32))
        elif re.fullmatch(r"-?\d+", item):
            values.append(np.array(int(item) & 0xFFFFFFFF, np.uint32).view(F32))
        else:
            values.append(F32(float(item)))
    return [F32(v) for v in values]


class Operand:
    __slots__ = ("kind", "index", "sub", "swizzle", "neg", "abs", "literal")

    def __init__(self, text):
        match = OPERAND.fullmatch(text.strip())
        if not match:
            raise ValueError(f"Unsupported shader operand {text!r}.")
        self.neg, self.abs = bool(match["neg"]), bool(match["abs"])
        self.literal, self.sub = None, None
        if match["lit"] is not None:
            self.kind, self.index = "l", 0
            self.literal = literal_bits(match["lit"])
            self.swizzle = "xyzw" if len(self.literal) == 4 else "xxxx"
            if len(self.literal) == 1:
                self.literal = self.literal * 4
        elif match["null"]:
            self.kind, self.index, self.swizzle = "null", 0, ""
        elif match["res"]:
            self.kind, self.index = match["res"], int(match["slot"])
            self.swizzle = match["rswz"] or "xyzw"
        else:
            self.kind = match["reg"]
            self.index = int(match["index"]) if match["index"] else 0
            if match["sub"] is not None:
                sub = match["sub"].replace(" ", "")
                self.sub = int(sub) if sub.isdigit() else sub
            self.swizzle = match["swz"] or "xyzw"


class Program:
    def __init__(self, code):
        self.text = disassemble(code)
        header, _, body = self.text.partition("\nps_5_0\n")
        if not body:
            raise ValueError("Only pixel shaders are supported.")
        self.variables = {}  # cbuffer variable -> (buffer slot, float offset, size in floats, used)
        bindings = {name: (kind, int(slot)) for name, kind, slot in re.findall(
            r"^// (\S+)\s+(cbuffer|texture|sampler)\s+\S+\s+\S+\s+[a-z]+(\d+)\s+\d+", header, re.M)}
        self.textures = {slot: name for name, (kind, slot) in bindings.items() if kind == "texture"}
        for block, contents in re.findall(r"^// cbuffer (\S+)\n// \{\n(.*?)^// \}", header, re.M | re.S):
            slot = bindings.get(block, (None, None))[1]
            for name, offset, size, flag in re.findall(
                    r"^//\s+(?:\w+ )+(\w+);\s*// Offset:\s+(\d+) Size:\s+(\d+)( \[unused\])?", contents, re.M):
                self.variables[name] = (slot, int(offset) // 4, int(size) // 4, not flag)
        # Compiled default values, e.g. "//   float CharacterMask; ... \n//      = 0x00000000"
        self.defaults = {name: np.array([int(word, 16) for word in words.split()], np.uint32).view(F32)
                         for name, words in re.findall(
                             r"^//\s+(?:\w+ )+(\w+);[^\n]*\n//\s+= ((?:0x[0-9a-f]{8}\s*)+)", header, re.M)}
        inputs, outputs = header.split("// Input signature:")[1].split("// Output signature:")
        # (semantic, index) -> (register, used components)
        self.inputs = {(name.upper(), int(index)): (int(register), used) for name, index, mask, register, used in
                       re.findall(r"^// (\w+)\s+(\d+)\s+([xyzw]+)\s+(\d+)\s+\w+\s+\w+\s*([xyzw]*)\s*$", inputs, re.M)}
        self.outputs = {(name.upper(), int(index)): int(register) for name, index, mask, register in re.findall(
            r"^// (\w+)\s+(\d+)\s+([xyzw]+)\s+(\d+)", outputs, re.M)}
        self.temps = 0
        self.instructions = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            opcode, _, rest = line.partition(" ")
            if opcode == "dcl_temps":
                self.temps = int(rest)
                continue
            if opcode.startswith(("dcl_indexableTemp", "dcl_immediateConstantBuffer", "loop", "switch", "call")):
                raise ValueError(f"Unsupported shader construct {opcode}.")
            if opcode.startswith("dcl_"):
                continue
            # e.g. sample_indexable(texture2d)(float,float,float,float) or mul_sat
            saturate = opcode.endswith("_sat")
            base = opcode.removesuffix("_sat").split("(")[0].removesuffix("_indexable")
            self.instructions.append((base, saturate, tuple(Operand(part) for part in split_operands(rest))))


# ---------------------------------------------------------------- textures

class Texture:
    """A sampled texture: mip pyramid of (H, W, 4) float32 arrays, or six cube faces."""

    def __init__(self, levels, address=("wrap", "wrap"), cube=False):
        self.levels, self.address, self.cube = levels, address, cube

    @staticmethod
    def pyramid(image):
        levels = [np.ascontiguousarray(image, dtype=F32)]
        while max(levels[-1].shape[:2]) > 1:
            top = levels[-1]
            h, w = top.shape[:2]
            if h > 1 and h % 2:
                top = np.concatenate((top, top[-1:]), 0)
            if w > 1 and w % 2:
                top = np.concatenate((top, top[:, -1:]), 1)
            if top.shape[0] > 1:
                top = (top[0::2] + top[1::2]) * F32(0.5)
            if top.shape[1] > 1:
                top = (top[:, 0::2] + top[:, 1::2]) * F32(0.5)
            levels.append(top)
        return levels

    def wrap(self, i, size, mode):
        if mode == "clamp":
            return np.clip(i, 0, size - 1)
        if mode == "mirror":
            period = np.mod(i, 2 * size)
            return np.where(period < size, period, 2 * size - 1 - period)
        return np.mod(i, size)

    def bilinear(self, image, u, v):
        h, w = image.shape[:2]
        x = u * w - 0.5
        y = v * h - 0.5
        x0, y0 = np.floor(x), np.floor(y)
        fx, fy = (x - x0).astype(F32)[..., None], (y - y0).astype(F32)[..., None]
        x0, y0 = x0.astype(np.int64), y0.astype(np.int64)
        xa, xb = self.wrap(x0, w, self.address[0]), self.wrap(x0 + 1, w, self.address[0])
        ya, yb = self.wrap(y0, h, self.address[1]) * w, self.wrap(y0 + 1, h, self.address[1]) * w
        flat = image.reshape(-1, image.shape[-1])
        gather = lambda index: flat.take(index, axis=0)
        top = gather(ya + xa) * (1 - fx) + gather(ya + xb) * fx
        bottom = gather(yb + xa) * (1 - fx) + gather(yb + xb) * fx
        return top * (1 - fy) + bottom * fy

    def sample2d(self, u, v, lod):
        u, v, lod = np.broadcast_arrays(np.asarray(u, F32), np.asarray(v, F32), np.asarray(lod, F32))
        lod = np.clip(np.nan_to_num(lod, nan=0, posinf=len(self.levels) - 1, neginf=0), 0, len(self.levels) - 1)
        low = np.floor(lod).astype(np.int64)
        result = np.zeros((*u.shape, 4), F32)
        for level in np.unique(low):
            pick = low == level
            colour = self.bilinear(self.levels[level], u[pick], v[pick])
            if level + 1 < len(self.levels):
                blend = (lod[pick] - level)[..., None]
                if np.any(blend > 0):
                    colour = colour * (1 - blend) + self.bilinear(self.levels[level + 1], u[pick], v[pick]) * blend
            result[pick] = colour
        return result

    def sample_cube(self, x, y, z):
        """D3D cube addressing: faces +X, -X, +Y, -Y, +Z, -Z."""
        x, y, z = np.broadcast_arrays(np.asarray(x, F32), np.asarray(y, F32), np.asarray(z, F32))
        ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
        result = np.zeros((*x.shape, 4), F32)
        major = np.where((ax >= ay) & (ax >= az), 0, np.where(ay >= az, 1, 2))
        faces = (
            (major == 0) & (x >= 0), (major == 0) & (x < 0), (major == 1) & (y >= 0),
            (major == 1) & (y < 0), (major == 2) & (z >= 0), (major == 2) & (z < 0),
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            coords = ((-z / ax, -y / ax), (z / ax, -y / ax), (x / ay, z / ay),
                      (x / ay, -z / ay), (x / az, -y / az), (-x / az, -y / az))
            for face, pick, (s, t) in zip(self.levels, faces, coords):
                if np.any(pick):
                    clamp = Texture([face], ("clamp", "clamp"))
                    result[pick] = clamp.bilinear(face, (s[pick] + 1) * 0.5, (t[pick] + 1) * 0.5)
        return result


# ---------------------------------------------------------------- execution

def bits(value, kind):
    return np.asarray(value, F32).view(kind)


def as_float(value, kind):
    return np.asarray(value).astype(kind, copy=False).view(F32)


class Machine:
    def __init__(self, program, shape):
        self.program = program
        self.shape = shape  # (height, width) of the texel grid
        self.trace = None   # a lane index to print each instruction's result for, when debugging

    def derivative(self, value, axis):
        value = np.asarray(value, F32)
        if value.ndim == 0:
            return F32(0)
        grid = value.reshape(self.shape)
        # Pixel quads share one difference per pair of rows/columns.
        if axis == 1:
            pairs = grid[:, 1::2] - grid[:, 0::2]
            out = np.repeat(pairs, 2, axis=1)[:, :grid.shape[1]]
        else:
            pairs = grid[1::2] - grid[0::2]
            out = np.repeat(pairs, 2, axis=0)[:grid.shape[0]]
        return out.reshape(-1)

    def run(self, inputs, constants, textures, varying=frozenset(), memo=None,
            varying_constants=frozenset(), varying_textures=frozenset()):
        """Execute the shader over the grid.

        inputs: {register: [4 components]}, constants: {slot: float32 array}, textures: {slot: Texture}.
        varying: input registers, varying_constants: (buffer slot, register) pairs and
        varying_textures: texture slots that differ between calls sharing `memo`. Results
        that depend on none of them are reused from `memo`.
        """
        program = self.program
        temps = [[F32(0)] * 4 for _ in range(program.temps)]
        tainted = [[False] * 4 for _ in range(program.temps)]
        outputs = {register: [F32(0)] * 4 for register in program.outputs.values()}
        size = self.shape[0] * self.shape[1]
        active = True            # bool or boolean array: lanes still executing
        killed = np.zeros(size, bool)
        stack = []
        recording = None

        def depends(operand):
            if operand.kind == "v":
                return operand.index in varying
            if operand.kind == "r":
                return any(tainted[operand.index][COMPONENTS.index(c)] for c in operand.swizzle)
            if operand.kind == "cb":
                return (operand.index, operand.sub) in varying_constants
            if operand.kind == "t":
                return operand.index in varying_textures
            return False

        def fetch(operand, component, integer=False):
            kind = operand.kind
            # A single-component select (r0.x, cb0[3].w) is replicated.
            swizzle = operand.swizzle
            c = COMPONENTS.index(swizzle[component] if component < len(swizzle) else swizzle[-1])
            if kind == "l":
                value = operand.literal[c]
            elif kind == "r":
                value = temps[operand.index][c]
            elif kind == "v":
                value = inputs[operand.index][c]
            elif kind == "cb":
                index = operand.sub
                if not isinstance(index, int):
                    raise ValueError("Dynamically indexed constants are not supported.")
                value = constants[operand.index][index * 4 + c]
            else:
                raise ValueError(f"Unsupported source operand {kind}.")
            if operand.abs or operand.neg:
                if integer:
                    value = bits(value, np.int32)
                    value = np.abs(value) if operand.abs else value
                    value = (-value if operand.neg else value).astype(np.int32).view(F32)
                else:
                    value = np.abs(value) if operand.abs else value
                    value = -value if operand.neg else value
            return value

        def store(operand, component, value, saturate):
            if operand.kind == "null":
                return
            if saturate:
                value = np.clip(np.nan_to_num(value, nan=0.0), 0, 1)
            value = np.asarray(value, F32)
            bank = temps[operand.index] if operand.kind == "r" else outputs[operand.index]
            if active is not True:
                value = np.where(active, value, bank[component])
            bank[component] = value if value.ndim else F32(value)
            if recording is not None:
                recording.append((operand, component, bank[component]))

        def written(operand):
            return [COMPONENTS.index(c) for c in operand.swizzle] if operand.kind != "null" else []

        def mask_of(value):
            value = bits(value, np.uint32)
            return value != 0

        np.seterr(all="ignore")
        for step, (opcode, saturate, ops) in enumerate(program.instructions):
            if opcode in ("if_nz", "if_z"):
                condition = mask_of(fetch(ops[0], 0))
                if opcode == "if_z":
                    condition = ~condition
                stack.append(active)
                active = active & condition if active is not True else condition
                if np.ndim(active) == 0:
                    active = bool(active) or False
                continue
            if opcode == "else":
                parent = stack[-1]
                taken = active
                inverse = ~np.asarray(taken) if np.ndim(taken) else not taken
                active = parent & inverse if parent is not True else inverse
                if np.ndim(active) == 0:
                    active = bool(active)
                continue
            if opcode == "endif":
                active = stack.pop()
                continue
            if active is False or (np.ndim(active) and not active.any()):
                continue
            if opcode in ("ret", "retc_nz", "retc_z"):
                if opcode != "ret":
                    condition = mask_of(fetch(ops[0], 0))
                    condition = ~condition if opcode == "retc_z" else condition
                    remaining = (active if active is not True else True) & ~condition
                    active = bool(remaining) if np.ndim(remaining) == 0 else remaining
                    continue
                if not stack:
                    break
                active = False
                continue
            if opcode in ("discard_nz", "discard_z"):
                condition = mask_of(fetch(ops[0], 0))
                if opcode == "discard_z":
                    condition = ~condition
                condition = np.broadcast_to(condition & (active if active is not True else True), (size,))
                killed |= condition
                continue
            targets = ops[:2] if opcode == "sincos" else ops[:1]
            pure = memo is not None and active is True and not any(map(depends, ops[len(targets):]))
            if pure and step in memo:
                for operand, component, value in memo[step]:
                    (temps[operand.index] if operand.kind == "r" else outputs[operand.index])[component] = value
            else:
                recording = [] if pure else None
                self.execute(opcode, saturate, ops, fetch, store, written, textures)
                if pure:
                    memo[step], recording = recording, None
            for operand in targets:
                if operand.kind == "r":
                    for c in written(operand):
                        tainted[operand.index][c] = not pure
            if self.trace is not None and ops and ops[0].kind == "r":
                lane = lambda v: float(np.asarray(v).ravel()[self.trace] if np.ndim(v) else v)
                print(opcode, ops[0].index, ops[0].swizzle, [round(lane(temps[ops[0].index][i]), 4) for i in range(4)])

        colour = {key: [np.broadcast_to(np.asarray(v, F32), (size,)) for v in outputs[register]]
                  for key, register in program.outputs.items()}
        return colour, killed

    def execute(self, opcode, saturate, ops, fetch, store, written, textures):
        dest, sources = ops[0], ops[1:]
        if opcode in ("sample", "sample_l", "sample_b", "sample_d", "sample_lz"):
            coords, texture = sources[0], textures[sources[1].index]
            if texture.cube:
                texel = texture.sample_cube(*(fetch(coords, i) for i in range(3)))
            else:
                u, v = fetch(coords, 0), fetch(coords, 1)
                if opcode == "sample_l":
                    lod = fetch(sources[3], 0)
                elif opcode == "sample_lz":
                    lod = F32(0)
                else:
                    if opcode == "sample_d":
                        gx = [fetch(sources[3], i) for i in range(2)]
                        gy = [fetch(sources[4], i) for i in range(2)]
                    else:
                        gx = [self.derivative(u, 1), self.derivative(v, 1)]
                        gy = [self.derivative(u, 0), self.derivative(v, 0)]
                    h, w = texture.levels[0].shape[:2]
                    rho = np.maximum(np.hypot(gx[0] * w, gx[1] * h), np.hypot(gy[0] * w, gy[1] * h))
                    lod = np.log2(np.maximum(rho, F32(1e-8)))
                    if opcode == "sample_b":
                        lod = lod + fetch(sources[3], 0)
                texel = texture.sample2d(u, v, lod)
            resource = sources[1].swizzle
            for c in written(dest):
                channel = texel[..., COMPONENTS.index(resource[c])]
                store(dest, c, channel.reshape(-1) if channel.ndim else channel, saturate)
            return
        if opcode == "resinfo":
            texture = textures[sources[1].index]
            h, w = texture.levels[0].shape[:2]
            info = [F32(w), F32(h), F32(1), F32(len(texture.levels))]
            for c in written(dest):
                store(dest, c, info[COMPONENTS.index(sources[1].swizzle[c])], False)
            return
        if opcode in ("dp2", "dp3", "dp4"):
            n = int(opcode[2])
            total = sum(fetch(sources[0], i) * fetch(sources[1], i) for i in range(n))
            for c in written(dest):
                store(dest, c, total, saturate)
            return
        if opcode == "sincos":
            angles = {c: fetch(ops[2], c) for c in range(4) if c in written(ops[0]) or c in written(ops[1])}
            for c, angle in angles.items():
                if c in written(ops[0]):
                    store(ops[0], c, np.sin(angle), saturate)
                if c in written(ops[1]):
                    store(ops[1], c, np.cos(angle), saturate)
            return
        # Read every source component before writing: a destination may alias a source.
        results = [(c, self.scalar(opcode, sources, c, fetch)) for c in written(dest)]
        for c, value in results:
            store(dest, c, value, saturate)

    def scalar(self, opcode, sources, c, fetch):
        f = lambda i: fetch(sources[i], c)
        if opcode == "mov":
            return f(0)
        if opcode == "add":
            return f(0) + f(1)
        if opcode == "mul":
            return f(0) * f(1)
        if opcode == "mad":
            return f(0) * f(1) + f(2)
        if opcode == "div":
            return f(0) / f(1)
        if opcode == "min":
            return np.minimum(f(0), f(1))
        if opcode == "max":
            return np.maximum(f(0), f(1))
        if opcode == "rsq":
            return 1 / np.sqrt(f(0))
        if opcode == "sqrt":
            return np.sqrt(f(0))
        if opcode == "rcp":
            return 1 / f(0)
        if opcode == "log":
            return np.log2(f(0))
        if opcode == "exp":
            return np.exp2(f(0))
        if opcode == "frc":
            value = f(0)
            return value - np.floor(value)
        if opcode == "round_ni":
            return np.floor(f(0))
        if opcode == "round_pi":
            return np.ceil(f(0))
        if opcode == "round_z":
            return np.trunc(f(0))
        if opcode == "round_ne":
            return np.round(f(0))
        if opcode in ("deriv_rtx", "deriv_rtx_coarse", "deriv_rtx_fine"):
            return self.derivative(f(0), 1)
        if opcode in ("deriv_rty", "deriv_rty_coarse", "deriv_rty_fine"):
            return self.derivative(f(0), 0)
        if opcode in ("lt", "ge", "eq", "ne"):
            a, b = f(0), f(1)
            result = {"lt": a < b, "ge": a >= b, "eq": a == b, "ne": a != b}[opcode]
            return as_float(np.where(result, np.uint32(0xFFFFFFFF), np.uint32(0)), np.uint32)
        if opcode == "movc":
            condition = bits(f(0), np.uint32) != 0
            return np.where(condition, f(1), f(2))
        if opcode in ("and", "or", "xor"):
            a, b = bits(f(0), np.uint32), bits(f(1), np.uint32)
            return as_float({"and": a & b, "or": a | b, "xor": a ^ b}[opcode], np.uint32)
        if opcode == "not":
            return as_float(~bits(f(0), np.uint32), np.uint32)
        if opcode in ("iadd", "imul", "ishl", "ishr", "imin", "imax", "ilt", "ige", "ieq", "ine", "ineg"):
            a = bits(fetch(sources[0], c, True), np.int32)
            b = bits(fetch(sources[1], c, True), np.int32) if len(sources) > 1 else None
            if opcode in ("ilt", "ige", "ieq", "ine"):
                result = {"ilt": a < b, "ige": a >= b, "ieq": a == b, "ine": a != b}[opcode]
                return as_float(np.where(result, np.uint32(0xFFFFFFFF), np.uint32(0)), np.uint32)
            result = {"iadd": lambda: a + b, "imul": lambda: a * b, "ishl": lambda: a << (b & 31),
                      "ishr": lambda: a >> (b & 31), "imin": lambda: np.minimum(a, b),
                      "imax": lambda: np.maximum(a, b), "ineg": lambda: -a}[opcode]()
            return as_float(np.asarray(result, np.int32), np.int32)
        if opcode in ("ushr", "ult", "uge", "umin", "umax"):
            a, b = bits(f(0), np.uint32), bits(f(1), np.uint32)
            if opcode in ("ult", "uge"):
                result = a < b if opcode == "ult" else a >= b
                return as_float(np.where(result, np.uint32(0xFFFFFFFF), np.uint32(0)), np.uint32)
            result = {"ushr": lambda: a >> (b & 31), "umin": lambda: np.minimum(a, b),
                      "umax": lambda: np.maximum(a, b)}[opcode]()
            return as_float(np.asarray(result, np.uint32), np.uint32)
        if opcode == "ftoi":
            return as_float(np.asarray(np.trunc(np.nan_to_num(f(0))), np.int32), np.int32)
        if opcode == "ftou":
            return as_float(np.asarray(np.trunc(np.clip(np.nan_to_num(f(0)), 0, None)), np.uint32), np.uint32)
        if opcode == "itof":
            return np.asarray(bits(fetch(sources[0], c, True), np.int32), F32)
        if opcode == "utof":
            return np.asarray(bits(f(0), np.uint32), F32)
        raise ValueError(f"Unsupported shader instruction {opcode}.")
