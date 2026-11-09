"""Bake Blender material maps by running a material's compiled game shaders.

Every Legendary Edition character material renders through BioWare shaders
whose material graph is stripped from the cooked packages; only the compiled
pixel shaders remain. This module evaluates the material's uniform expressions
into the shader constants, binds its textures, and runs two of its shaders over
the texture grid:

* the base pass (ambient + emissive), whose second render target carries the
  material's tangent-space normal, and
* the directional light pass, the exact per-light response F(L, E) of the
  material, including BioWare's custom lighting.

F is probed with controlled light (L) and view (E) directions around each
texel's normal and decomposed into what a Principled BSDF can express: diffuse
albedo, up to two Phong lobes (fitted exactly with Prony's method on a
geometric sweep of R.L), back-light transmission, emission, opacity and the
material's own cube-map reflection weight.
"""
from concurrent.futures import ThreadPoolExecutor
import math
import os

import numpy as np
from PIL import Image

from mele_dxbc import F32, Machine, Program, Texture

LIGHT = "TLightPixelShaderFDirectionalLightPolicyFNoStaticShadowingPolicy"
BASE = ("TBasePassPixelShaderFNoLightMapPolicySkyLight", "TBasePassPixelShaderFNoLightMapPolicyNoSkyLight")
VERTEX_FACTORIES = ("FGPUSkinVertexFactory", "FGPUSkinMorphVertexFactory", "FGPUSkinDecalVertexFactory")
FOLDED_MATH = {0: np.add, 1: np.subtract, 2: np.multiply, 3: np.divide}

# Interpolator semantics written by the engine's vertex shaders
# (LightVertexShader.usf / BasePassVertexShader.usf); the same for every material.
UV_SETS = ("TEXCOORD", 0), ("TEXCOORD", 1), ("TEXCOORD", 2), ("TEXCOORD", 3)
TANGENT_X, TANGENT_Z = ("TEXCOORD", 10), ("TEXCOORD", 11)
LIGHT_VECTOR, CAMERA_VECTOR, SKY_VECTOR = ("TEXCOORD", 4), ("TEXCOORD", 6), ("TEXCOORD", 7)

VIEW = math.acos(2 / 3)       # the cosine-weighted mean view elevation over the hemisphere
BEHIND = math.radians(120)    # light from behind the surface for transmission
SWEEP = 0.98                  # geometric ratio of the R.L sweep (lobe exponents up to ~500)
NARROW = 0.998                # single sample that resolves exponents up to ~5000


# ---------------------------------------------------------------- shader selection

def shader_set(shader_map):
    """Pick the skinned-mesh light and base pass shaders of a shader map."""
    factories = [vf for vf in VERTEX_FACTORIES if vf in shader_map.mesh] + sorted(shader_map.mesh)
    for vf in factories:
        rows = shader_map.mesh[vf]
        base = next((rows[name] for name in BASE if name in rows), None)
        if base or LIGHT in rows:
            return vf, rows.get(LIGHT), base
    return None, None, None


# ---------------------------------------------------------------- uniforms

def evaluate(expression, scalars, vectors):
    kind = expression[0]
    if kind == "Constant":
        return np.array(expression[1], F32)
    if kind == "VectorParameter":
        return np.array(vectors.get(expression[1].casefold(), expression[2]), F32)
    if kind == "ScalarParameter":
        return np.full(4, scalars.get(expression[1].casefold(), expression[2]), F32)
    if kind in ("Time", "RealTime", "FractionOfEffectEnabled"):
        # A still bake at time zero with BioWare's runtime effect layers off.
        return np.zeros(4, F32)
    args = [evaluate(child, scalars, vectors) for child in expression[1:-1]]
    tail = expression[-1]
    with np.errstate(all="ignore"):
        if kind == "FoldedMath":
            if tail[0] == 4:
                return np.full(4, np.dot(args[0][:3], args[1][:3]), F32)
            return FOLDED_MATH[tail[0]](args[0], args[1]).astype(F32)
        if kind == "AppendVector":
            count = int.from_bytes(tail, "little")
            return np.concatenate((args[0][:count], args[1][:4 - count])).astype(F32)
        if kind == "Clamp":
            return np.clip(args[0], args[1], args[2])
        if kind == "Min":
            return np.minimum(args[0], args[1])
        if kind == "Max":
            return np.maximum(args[0], args[1])
        if kind == "Fmod":
            return np.fmod(args[0], args[1]).astype(F32)
        if kind == "Abs":
            return np.abs(args[0])
        if kind == "Ceil":
            return np.ceil(args[0])
        if kind == "Floor":
            return np.floor(args[0])
        if kind in ("Frac", "Periodic"):
            return (args[0] - np.floor(args[0])).astype(F32)
        if kind == "SquareRoot":
            return np.sqrt(args[0])
        if kind == "Sine":
            value = np.cos(args[0][0]) if any(tail) else np.sin(args[0][0])
            return np.array((value, 0, 0, 0), F32)
    raise ValueError(f"Unsupported uniform expression {kind}.")


def uniform_constants(shader_map, scalars, vectors):
    values = {f"UniformPixelVector_{i}": evaluate(e, scalars, vectors) for i, e in enumerate(shader_map.vectors)}
    packed = [evaluate(e, scalars, vectors)[0] for e in shader_map.scalars]
    # LE1/LE2 shaders declare each scalar (UniformPixelScalar_N); LE3 packs four (UniformPixelScalars_N).
    values.update({f"UniformPixelScalar_{i}": np.array([value], F32) for i, value in enumerate(packed)})
    packed += [0.0] * (-len(packed) % 4)
    for i in range(0, len(packed), 4):
        values[f"UniformPixelScalars_{i // 4}"] = np.array(packed[i:i + 4], F32)
    return values


# ---------------------------------------------------------------- textures

def to_linear(rgb):
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4).astype(F32)


def to_srgb(rgb):
    rgb = np.clip(rgb, 0, 1)
    return np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * rgb ** (1 / 2.4) - 0.055)


def load_image(path, srgb):
    image = Image.open(path)
    mode = image.mode
    if mode in ("I;16", "I;16B", "I"):
        pixels = np.asarray(image, F32)[..., None].repeat(3, 2) / 65535
        pixels = np.concatenate((pixels, np.ones_like(pixels[..., :1])), 2)
    else:
        pixels = np.asarray(image.convert("RGBA"), F32) / 255
    if srgb:
        pixels[..., :3] = to_linear(pixels[..., :3])
    return pixels


def texture(binding):
    address = tuple("clamp" if clamp else "wrap" for clamp in binding.get("clamp", (False, False)))
    if binding["kind"] == "TextureCube":
        faces = [load_image(path, binding.get("srgb", True)) for path in binding["faces"]]
        return Texture(faces, cube=True)
    return Texture(Texture.pyramid(load_image(binding["path"], binding.get("srgb", True))), address)


def constant_texture(value, cube=False):
    face = np.full((1, 1, 4), value, F32)
    return Texture([face] * 6 if cube else [face], cube=cube)


# ---------------------------------------------------------------- probing

def frame(normal):
    """An orthonormal tangent-space basis (T, B, N) around each texel normal."""
    n = normal
    helper = np.where(np.abs(n[1:2]) < 0.9, np.array([[0], [1], [0]], F32), np.array([[1], [0], [0]], F32))
    t = np.cross(helper, n, axis=0)
    t /= np.linalg.norm(t, axis=0, keepdims=True)
    return t, np.cross(n, t, axis=0), n


def direction(basis, elevation, azimuth):
    t, b, n = basis
    return (math.sin(elevation) * np.cos(azimuth) * t + math.sin(elevation) * np.sin(azimuth) * b
            + math.cos(elevation) * n).astype(F32)


def sweep_azimuth(cosine):
    """Azimuth of L (at the view elevation) that gives R.L = cosine for E at azimuth 0."""
    c2, s2 = math.cos(VIEW) ** 2, math.sin(VIEW) ** 2
    return math.acos(max(-1.0, min(1.0, (c2 - cosine) / s2)))


class Material:
    def __init__(self, shader_cache, shader_map, scalars, vectors, bindings, log, config=None):
        self.log = log
        config = config or {}
        # [WrapLighting] in BioEngine.ini; the light pass reads DirectScale from .x.
        self.wrap = [float(config.get(("wraplighting", key), ["0"])[0]) for key in ("directscale", "indirectscale")]
        self.unset = set()
        self.vf, light, base = shader_set(shader_map)
        if base is None:
            raise ValueError(f"{shader_map.name} has no base pass shader for skinned meshes.")
        self.base = Program(shader_cache.bytecode(base))
        self.light = Program(shader_cache.bytecode(light)) if light else None
        self.uniforms = uniform_constants(shader_map, scalars, vectors)
        self.textures = {}
        for name, binding in bindings.items():
            self.textures[name] = texture(binding) if binding else None
        sizes = [t.levels[0].shape[:2] for t in self.textures.values() if t and not t.cube]
        self.size = max(sizes, key=lambda hw: hw[0] * hw[1]) if sizes else (256, 256)
        self.size = tuple(max(2, s + s % 2) for s in self.size)
        for program in filter(None, (self.base, self.light)):
            if any(key in program.inputs and program.inputs[key][1] for key in UV_SETS[1:]):
                log(f"Warning: {shader_map.name} samples a second UV set; it is baked in the first UV set.")

    # ------------------------------------------------------------ one shader run

    def constants(self, program, engine):
        buffers = {}
        for name, (slot, offset, size, used) in program.variables.items():
            buffer = buffers.setdefault(slot, np.zeros(4096, F32))
            value = self.uniforms.get(name, engine.get(name))
            if value is None:
                value = program.defaults.get(name)
            if value is None and used and name not in self.unset:
                self.unset.add(name)
                self.log(f"Note: shader constant {name} is not set by the bake; it reads zero.")
            if value is not None:
                value = np.asarray(value, F32).ravel()[:size]
                buffer[offset:offset + len(value)] = value
        return buffers

    def bind(self, program, cube_value=None):
        bound = {}
        for slot, name in program.textures.items():
            if name.startswith(("Texture2D_", "TextureCube_")):
                image = self.textures.get(name)
                cube = name.startswith("TextureCube_")
                if cube and cube_value is not None:
                    image = constant_texture(cube_value, cube=True)
                bound[slot] = image or constant_texture(0, cube)
            else:
                # Engine textures (shadow attenuation): unshadowed.
                bound[slot] = constant_texture(1)
        return bound

    def run(self, program, rows, engine, vectors, cube_value=None, varying=(), memo=None, varying_constants=()):
        """Evaluate a shader over texel rows. vectors: semantic -> (3, n) arrays.

        Calls sharing `memo` may differ only in the `varying` semantics, the named
        `varying_constants` and the cube textures.
        """
        height, width = self.size
        y0, y1 = rows
        v, u = np.mgrid[y0:y1, 0:width].astype(F32)
        u = ((u + 0.5) / width).ravel()
        v = ((v + 0.5) / height).ravel()
        one = F32(1)
        inputs = {}
        for key, (register, _) in program.inputs.items():
            if key in UV_SETS:
                value = [u, v, F32(0), one]
            elif key == TANGENT_X:
                value = [one, F32(0), F32(0), one]
            elif key == TANGENT_Z:
                # With binormal sign +1 the engine's frame is the identity: world == tangent space
                # (checked: SV_Target2 then returns the decoded tangent normal unchanged).
                value = [F32(0), F32(0), one, one]
            elif key in vectors:
                value = [*vectors[key], one]
            elif key[0] == "COLOR":
                value = [one] * 4
            elif key[0] == "SV_ISFRONTFACE":
                value = [np.array(0xFFFFFFFF, np.uint32).view(F32)] * 4
            else:
                value = [F32(0)] * 4
            inputs[register] = value
        machine = Machine(program, (y1 - y0, width))
        registers = frozenset(program.inputs[key][0] for key in varying if key in program.inputs)
        constant_registers = set()
        for name in varying_constants:
            if name in program.variables:
                slot, offset, size, _ = program.variables[name]
                constant_registers.update((slot, r) for r in range(offset // 4, (offset + size + 3) // 4))
        cubes = frozenset(slot for slot, name in program.textures.items() if name.startswith("TextureCube_"))
        return machine.run(inputs, self.constants(program, engine), self.bind(program, cube_value),
                           varying=registers, memo=memo, varying_constants=frozenset(constant_registers),
                           varying_textures=cubes if memo is not None else frozenset())

    # ------------------------------------------------------------ bake

    def bake(self, tile_rows=64):
        height, width = self.size
        tiles = [(start, min(height, start + tile_rows)) for start in range(0, height, tile_rows)]
        # numpy releases the GIL in array arithmetic and gathers, so tiles run in parallel threads.
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            results = list(pool.map(self.bake_rows, tiles))
        return {key: np.concatenate([part[key] for part in results], axis=-1).reshape(-1, height, width)
                for key in results[0]}

    def bake_rows(self, rows):
        n = (rows[1] - rows[0]) * self.size[1]
        up = np.array([[0], [0], [1]], F32) * np.ones((1, n), F32)
        engine = {"WorldToViewMatrix": np.eye(3, 4, dtype=F32), "LocalToWorld": np.eye(4, dtype=F32),
                  "LocalToWorldMatrix": np.eye(3, 4, dtype=F32), "WorldToLocalMatrix": np.eye(3, 4, dtype=F32),
                  "TwoSidedSign": [1.0], "UpperSkyColor": [0, 0, 0], "LowerSkyColor": [0, 0, 0],
                  "AmbientColorAndSkyFactor": [0, 0, 0, 0], "WrapLightingParameters": [*self.wrap, 0, 0]}
        flat = {CAMERA_VECTOR: up, SKY_VECTOR: up}
        shared = {}
        base = lambda settings, cube: self.run(self.base, rows, settings, flat, cube_value=cube, memo=shared,
                                               varying_constants=("WorldToViewMatrix", "AmbientColorAndSkyFactor"))
        colour, killed = base(engine, 0)
        result = {"emission": np.stack(colour[("SV_TARGET", 0)][:3]), "killed": killed[None],
                  "opacity": colour[("SV_TARGET", 0)][3][None]}
        normal = None
        if ("SV_TARGET", 2) in colour:
            xy = np.stack(colour[("SV_TARGET", 2)][:2]) * 2 - 1
            swapped = dict(engine, WorldToViewMatrix=np.array([[0, 0, 1, 0], [0, 1, 0, 0], [1, 0, 0, 0]], F32))
            z = base(swapped, 0)[0][("SV_TARGET", 2)][0] * 2 - 1
            normal = np.concatenate((xy, z[None]))
            normal /= np.maximum(np.linalg.norm(normal, axis=0, keepdims=True), 1e-6)
        else:
            normal = up.copy()
            result["normal_missing"] = np.ones((1, n), F32)
        result["normal"] = normal
        if any(name.startswith("TextureCube_") for name in self.base.textures.values()):
            ambient = dict(engine, AmbientColorAndSkyFactor=[1, 1, 1, 0])
            white = base(ambient, 1)[0][("SV_TARGET", 0)]
            black = base(ambient, 0)[0][("SV_TARGET", 0)]
            result["reflection"] = np.stack(white[:3]) - np.stack(black[:3])
        if self.light is None:
            return result

        basis = frame(normal)
        view = direction(basis, VIEW, 0)
        light_engine = dict(engine, LightColor=[1, 1, 1], bReceiveDynamicShadows=[0], bEnableDistanceShadowFading=[0])
        memo = {}

        def respond(elevation, azimuth):
            vectors = {LIGHT_VECTOR: direction(basis, elevation, azimuth), CAMERA_VECTOR: view}
            out = self.run(self.light, rows, light_engine, vectors, cube_value=0,
                           varying=(LIGHT_VECTOR,), memo=memo)[0][("SV_TARGET", 0)]
            return np.stack(out[:3]).astype(np.float64)

        # Specular: R.L sweep at constant N.L and view, relative to a light with R.L < 0.
        near = respond(VIEW, 0)
        samples = [respond(VIEW, sweep_azimuth(SWEEP ** k)) - near for k in range(4)]
        narrow = respond(VIEW, sweep_azimuth(NARROW)) - near
        result.update(fit_lobes(samples, narrow))
        # Diffuse: the response to a light along the normal (N.L = 1, where R.L = N.E),
        # less the fitted lobes' exact contribution there.
        mirror = math.cos(VIEW)
        lobes = result["spec"] * mirror ** result["power"] + result["coat"] * mirror ** result["coat_power"]
        result["albedo"] = respond(0.0, 0) - lobes
        result["transmission"] = respond(BEHIND, 0) / abs(math.cos(BEHIND))
        return result


def fit_lobes(samples, narrow):
    """Fit y(c) = S1 c^P1 + S2 c^P2 to samples at c = SWEEP^k (k = 0..3) and c = NARROW."""
    y = np.stack(samples)                      # (4, 3, n)
    s = y.sum(axis=1)                          # (4, n)
    tiny = 1e-6
    single_power = np.log(np.clip(narrow.sum(0) / np.maximum(s[0], tiny), 1e-12, 1)) / math.log(NARROW)
    single_power = np.clip(single_power, 0, 10000)
    # Prony: s[k+2] = a s[k+1] + b s[k]  for k = 0, 1.
    det = s[1] * s[1] - s[0] * s[2]
    with np.errstate(all="ignore"):
        a = (s[2] * s[1] - s[0] * s[3]) / det
        b = (s[1] * s[3] - s[2] * s[2]) / det
        disc = a * a + 4 * b
        root = np.sqrt(np.maximum(disc, 0))
        r1, r2 = (a + root) / 2, (a - root) / 2
        valid = (np.abs(det) > 1e-10) & (disc > 0) & (r1 > 0) & (r1 < 1) & (r2 > 0) & (r2 < r1 * 0.95)
        w1 = (s[1] - r2 * s[0]) / (r1 - r2)
        w2 = s[0] - w1
        valid &= (w1 > tiny) & (w2 > tiny)
        p1 = np.log(r1) / math.log(SWEEP)
        p2 = np.log(r2) / math.log(SWEEP)
        # Keep the two-lobe fit only where it also predicts the narrow sample.
        predicted = w1 * NARROW ** p1 + w2 * NARROW ** p2
        valid &= np.abs(predicted - narrow.sum(0)) <= 0.02 * np.abs(narrow.sum(0)) + tiny
        # Per-channel lobe weights from c^0 and c^1: y0 = S1 + S2, y1 = S1 r1 + S2 r2.
        s1 = (y[1] - r2 * y[0]) / (r1 - r2)
        s2 = y[0] - s1
    valid = valid & np.all(s1 >= -tiny, axis=0) & np.all(s2 >= -tiny, axis=0)
    primary = np.where(valid, s1, y[0])
    secondary = np.where(valid, s2, 0)
    return {"spec": np.clip(primary, 0, None), "power": np.where(valid, p1, single_power)[None],
            "coat": np.clip(secondary, 0, None), "coat_power": np.where(valid, p2, 0)[None]}


# ---------------------------------------------------------------- output maps

def roughness(exponent):
    # Match the width of a Phong lobe in Blender's GGX roughness convention.
    return (2 / (np.maximum(exponent, 0) * 4 + 2)) ** 0.25


def save(folder, name, pixels, srgb=False):
    """pixels: (c, h, w) in [0, 1]; c = 1, 3 or 4."""
    data = np.clip(pixels, 0, 1)
    if srgb:
        data = data.copy()
        data[:3] = to_srgb(data[:3])
    data = np.moveaxis((data * 255 + 0.5).astype(np.uint8), 0, -1)
    mode = {1: "L", 3: "RGB", 4: "RGBA"}[data.shape[-1]]
    path = folder / f"{name}.png"
    Image.fromarray(data[..., 0] if mode == "L" else data, mode).save(path)
    return path.name


def bake_cubemap(cube, folder, name):
    """Convert the six D3D cube faces into a Blender equirectangular environment image."""
    edge = cube.levels[0].shape[0]
    width, height = edge * 4, edge * 2
    longitude = ((np.arange(width) + 0.5) / width - 0.5) * (2 * np.pi)
    latitude = (0.5 - (np.arange(height) + 0.5) / height) * np.pi
    lon, lat = np.meshgrid(longitude, latitude)
    # Blender environment direction, expressed in the game's world axes.
    x, y, z = np.cos(lon) * np.cos(lat), np.sin(lon) * np.cos(lat), np.sin(lat)
    colour = cube.sample_cube(x.ravel(), -y.ravel(), z.ravel())[:, :3].T.reshape(3, height, width)
    return save(folder, name, colour, srgb=True)


def write_maps(maps, folder, name, blend, cube):
    """Translate the probed responses into Principled BSDF inputs."""
    files, settings, notes = {}, {}, []
    albedo = maps["albedo"] if "albedo" in maps else np.zeros_like(maps["emission"])
    alpha = None
    if maps["killed"].any():
        alpha = (~maps["killed"]).astype(np.float32)
    elif blend in ("BLEND_Translucent", "BLEND_AlphaComposite"):
        alpha = np.clip(maps["opacity"], 0, 1)
    colour = np.clip(albedo, 0, 1)
    files["Base Color"] = save(folder, f"{name}_BaseColor",
                               np.concatenate((colour, alpha)) if alpha is not None else colour, srgb=True)
    if alpha is not None:
        settings["alpha"] = True
    if "normal_missing" in maps and maps["normal_missing"].all() and "spec" in maps:
        notes.append("its base pass writes no normal, so it is baked with the mesh normal")
    files["Normal"] = save(folder, f"{name}_Normal", maps["normal"] * 0.5 + 0.5)
    emission = maps["emission"]
    if emission.max() > 1e-3:
        scale = float(emission.max())
        files["Emission Color"] = save(folder, f"{name}_Emission", emission / scale, srgb=True)
        settings["Emission Strength"] = scale
    if "spec" in maps:
        # The game's lighting is A.(N.L) for diffuse albedo A, so a light response
        # S.cos^P(R.L) is the BRDF S.cos^P / (pi N.L) and reflects
        # (S / pi) * 2 pi / (P + 1) = 2 S / (P + 1) of the light: the lobe's F0.
        spec = maps["spec"] * 2 / (maps["power"] + 1)
        strength = spec.max(axis=0, keepdims=True)
        # A few texels at mask edges overshoot; the 99.9th percentile sets the scale.
        peak = float(np.clip(np.percentile(strength, 99.9), 1e-6, 0.9999))
        # Invert dielectric Fresnel at normal incidence; the map carries each
        # texel's fraction of the material's peak reflection coefficient.
        settings["IOR"] = (1 + math.sqrt(peak)) / (1 - math.sqrt(peak))
        files["Specular IOR Level"] = save(folder, f"{name}_Specular", strength / (2 * peak))
        files["Specular Tint"] = save(folder, f"{name}_SpecularTint", spec / np.maximum(strength, 1e-6))
        files["Roughness"] = save(folder, f"{name}_Roughness", roughness(maps["power"]))
        coat = maps["coat"] * 2 / (maps["coat_power"] + 1)
        if coat.max() > 1e-4:
            # Principled coat reflects 4% (IOR 1.5) at full weight.
            weight = coat.max(axis=0, keepdims=True)
            files["Coat Weight"] = save(folder, f"{name}_CoatWeight", weight / 0.04)
            files["Coat Tint"] = save(folder, f"{name}_CoatTint", coat / np.maximum(weight, 1e-6))
            files["Coat Roughness"] = save(folder, f"{name}_CoatRoughness", roughness(maps["coat_power"]))
        transmission = np.clip(maps["transmission"], 0, None)
        if transmission.max() > 1e-3:
            files["Transmission"] = save(folder, f"{name}_Transmission", np.clip(transmission, 0, 1))
    else:
        settings["unlit"] = True
    if "reflection" in maps and cube is not None and np.abs(maps["reflection"]).max() > 1e-3:
        reflection = np.clip(maps["reflection"], 0, None)
        scale = float(max(reflection.max(), 1e-6))
        files["Reflection Weight"] = save(folder, f"{name}_ReflectionWeight", reflection / scale)
        files["Reflection Cube"] = bake_cubemap(cube, folder, f"{name}_ReflectionCube")
        settings["Reflection Strength"] = scale
    return files, settings, notes
