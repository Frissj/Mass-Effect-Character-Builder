
"""Read Legendary Edition armour packages through Gildor's UE Viewer."""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from mele_characters import roster
import mele_shader_bake as bake
from mele_morph import GamePackages, transfer_positions

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "mele"
GAMES = ("LE1", "LE2", "LE3")
UMODEL_URL = "https://www.gildor.org/down/47/umodel/umodel_win32.zip?download=1"
UMODEL_SHA256 = "2cead261b360dbcc3e703f091837b415f868250dd6a73a154f4232d09e226ef8"
ARMOUR_PACKAGE = re.compile(r"biog_[a-z0-9]+_(?:arm|hgr|shp|frm|bod)_", re.I)
TRANSPARENT_BLENDS = ("BLEND_Translucent", "BLEND_Additive", "BLEND_Modulate", "BLEND_AlphaComposite")


def index_name(key):
    return re.sub(r"[^A-Za-z0-9_]+", "_", key)


def game_root():
    if override := os.environ.get("MELE_GAME"):
        return Path(override)
    steam = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
        except OSError:
            pass
    libraries = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        libraries.extend(Path(p.replace("\\\\", "\\")) for p in
                         re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8-sig")))
    installs = [p / "steamapps/common/Mass Effect Legendary Edition" for p in libraries]
    return next((p for p in installs if (p / "Game").is_dir()), installs[0])


def biogame(root, game):
    path = (root / "Game" / f"ME{game[-1]}" / "BioGame").resolve()
    if not (path / "CookedPCConsole").is_dir():
        raise ValueError(f"{game} game packages were not found under {path}. Choose the Legendary Edition install folder.")
    return path


def armour_packages(root):
    packages = [p.relative_to(root).as_posix() for p in root.rglob("*.pcc")
                if ARMOUR_PACKAGE.match(p.name)]
    return sorted(packages, key=lambda p: (not p.startswith("CookedPCConsole/"), p.casefold()))


def select_package(root, name):
    """Resolve a BioGame-relative path or a unique filename in the armour catalogue."""
    name = name.replace("\\", "/")
    path = (root / name).resolve()
    if path.is_relative_to(root) and path.is_file() and path.suffix.lower() == ".pcc":
        return path.relative_to(root).as_posix()
    matches = [p for p in armour_packages(root) if Path(p).stem.casefold() == name.lower().removesuffix(".pcc")]
    if len(matches) != 1:
        hint = "\n" + "\n".join(matches) if matches else " Use --list to see armour packages."
        raise ValueError(f"Choose a BioGame-relative .pcc path for {name}.{hint}")
    return matches[0]


def umodel_path(override, log):
    if override:
        path = Path(override).resolve()
        if not path.is_file():
            raise ValueError(f"UE Viewer was not found at {path}")
        return path
    tool_dir = DATA / "umodel-1590"
    exe = tool_dir / "umodel_64.exe"
    if exe.is_file():
        return exe
    log("Downloading UE Viewer build 1590 from Gildor…")
    request = urllib.request.Request(UMODEL_URL, headers={"Referer": "https://www.gildor.org/en/projects/umodel"})
    with urllib.request.urlopen(request, timeout=30) as response:
        archive = response.read(4 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != UMODEL_SHA256:
        raise ValueError("The UE Viewer download differs from the verified build. Pass --umodel with a local copy.")
    DATA.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DATA) as tmp, zipfile.ZipFile(io.BytesIO(archive)) as source:
        files = Path(tmp) / "umodel"
        files.mkdir()
        for name in ("umodel_64.exe", "SDL2_64.dll", "LICENSE.txt", "readme.txt"):
            (files / name).write_bytes(source.read(name))
        try:
            files.rename(tool_dir)
        except OSError:
            if not exe.is_file():
                raise
    return exe


def blender_path(override):
    installed = [path for pattern in (r"Bforartists\*\bforartists.exe", r"Blender Foundation\Blender*\blender.exe")
                 for path in sorted(Path(os.environ.get("ProgramFiles", r"C:\Program Files")).glob(pattern))]
    candidate = (override or shutil.which("blender") or shutil.which("bforartists")
                 or (installed[-1] if installed else None))
    if not candidate or not Path(candidate).is_file():
        raise ValueError("Blender or Bforartists was not found. Choose its executable.")
    return Path(candidate).resolve()


def run_tool(command, cwd, log=None):
    with subprocess.Popen([str(arg) for arg in command], cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        lines = []
        for line in process.stdout:
            lines.append(line.rstrip())
            if log:
                log(lines[-1])
        if process.wait():
            raise RuntimeError(f"{Path(command[0]).name} exited with code {process.returncode}\n"
                               + "\n".join(lines[-14:]))
    return lines


def read_package(tool, root, package, options, cwd):
    # UE Viewer resolves a literal package name by basename first. A path wildcard
    # forces full-path matching so a mod cannot silently shadow the chosen package.
    prefix, sep, name = package.rpartition("/")
    pattern = f"{prefix}{sep}?{name[1:]}"
    lines = run_tool([tool, f"-path={root.as_posix()}", *options, pattern], cwd)
    loaded = [line.split(" Ver:", 1)[0].removeprefix("Loading package: ")
              for line in lines if line.startswith("Loading package: ")]
    if not loaded or loaded[0].casefold() != package.casefold():
        raise RuntimeError(f"UE Viewer could not select {package}\n" + "\n".join(lines[-8:]))
    return lines


def meshes(root, package):
    pcc = GamePackages(root).open(package)
    names = []
    for indices in pcc.by_name.values():
        exports = [i for i in indices if i > 0 and pcc.entries[i].kind == "SkeletalMesh"]
        names.extend(pcc.entries[i].name if len(exports) == 1 else ".".join(pcc.object_path(i)) for i in exports)
    return sorted(names, key=str.casefold)


def stock_materials(game_data, package, mesh, morph, log):
    """Resolve each mesh material to its compiled shader map, parameters and bound textures."""
    materials = {}
    mesh_package = game_data.open(package)
    for index in game_data.mesh_materials(package, mesh):
        key = mesh_package.entries[index].name.casefold()
        record = game_data.material(package, index)
        shader_map = game_data.shader_map(record)
        scalars, vectors, texture_values = dict(record.scalars), dict(record.vectors), dict(record.texture_values)
        if morph:
            # The stock face morph's material overrides win over the instance chain.
            for name, value in morph.values.items():
                if isinstance(value, tuple):
                    vectors[name.casefold()] = (*value, *(1.0,) * (4 - len(value)))
                else:
                    scalars[name.casefold()] = value
            texture_values.update({name.casefold(): ref for name, ref in morph.textures.items()})
        entry = {"master": record.master, "blend": record.blend, "shader_map": shader_map,
                 "scalars": scalars, "vectors": vectors, "refs": {}, "sampling": {}}
        materials[key] = entry
        if shader_map is None:
            log(f"Warning: no compiled shader was found for {key} (master {record.master}).")
            continue
        slots = [(f"Texture2D_{i}", e) for i, e in enumerate(shader_map.textures)]
        slots += [(f"TextureCube_{i}", e) for i, e in enumerate(shader_map.cubes)]
        for slot, expression in slots:
            ref = None
            if expression[0] == "TextureParameter" and expression[1].casefold() in texture_values:
                ref = texture_values[expression[1].casefold()]
            elif expression[0] in ("Texture", "TextureParameter"):
                position = expression[-1]
                if 0 <= position < len(record.textures):
                    ref = record.textures[position]
            resolved = game_data.resolve(*ref) if ref and ref[1] else None
            if not resolved:
                name = ".".join(game_data.open(ref[0]).object_path(ref[1])) if ref and ref[1] else "none"
                log(f"Warning: {key} {slot} texture {name} is not in the installed packages; it samples black.")
                continue
            source, texture = resolved
            pcc = game_data.open(source)
            kind = pcc.entries[texture].kind
            if kind not in ("Texture2D", "TextureCube"):
                continue
            entry["refs"][slot] = (source, pcc.object_path(texture), kind)
            if kind == "Texture2D":
                props, _ = pcc.props(texture)
                entry["sampling"][slot] = {
                    "srgb": props.get("SRGB", b"\1") != b"\0",
                    "clamp": [axis in props and pcc.name(props[axis]) == "TA_Clamp" for axis in ("AddressX", "AddressY")]}
    return materials


def export_mesh(tool, root, package, mesh, scratch, textures, log, game_data, morph=None, *, apply_morph=False,
                keep_materials=()):
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", mesh):
        raise ValueError(f"Unsupported mesh name: {mesh}")
    log(f"Extracting {mesh} from {package}")
    geometry = scratch / "geometry"
    material_data = scratch / "materials"
    stock = stock_materials(game_data, package, mesh, morph, log)
    mesh_path = game_data.open(package).object_path(game_data.mesh(package, mesh))
    mesh_name = mesh_path[-1]

    def select(paths, object_path):
        expected = tuple(part.casefold() for part in object_path)
        matches = [p for p in paths if p.stem.casefold() == expected[-1]]
        qualified = [p for p in matches
                     if (*tuple(part.casefold() for part in p.parent.parts), p.stem.casefold())[-len(expected):] == expected]
        candidates = qualified or matches
        return candidates[0] if len(candidates) == 1 else None

    for folder, flag in ((geometry, "-gltf"), (material_data, "-png")):
        read_package(tool, root, package, ["-export", "-groups", f"-out={folder.as_posix()}",
                                          f"-obj={mesh_name}", flag], scratch)
    gltf = select(geometry.rglob("*.gltf"), mesh_path)
    if gltf is None:
        raise RuntimeError(f"UE Viewer did not resolve the export for {mesh}.")
    bones = {}
    if apply_morph:
        psk = select(material_data.rglob("*.psk"), mesh_path)
        if psk is None:
            raise ValueError(f"Stock face morph {morph.name} requires a matching PSK wedge map.")
        log(f"Applying stock face morph {morph.name} to {mesh}")
        try:
            transfer_positions(gltf, psk, morph.vertices)
        except ValueError as exc:
            log(f"Warning: {morph.name} geometry could not be transferred: {exc}")
        else:
            bones = morph.bones
    parsed = json.loads(gltf.read_text(encoding="utf-8"))
    if not parsed.get("skins"):
        raise RuntimeError(f"{mesh} has no exported skeleton.")
    if keep_materials:
        material_ids = {index for index, item in enumerate(parsed["materials"])
                        if item.get("name", "").casefold() in keep_materials}
        if not material_ids:
            raise ValueError(f"No verified body surfaces were found on {mesh}.")
        for section in parsed["meshes"]:
            section["primitives"] = [primitive for primitive in section["primitives"]
                                     if primitive.get("material") in material_ids]
            if not section["primitives"]:
                raise ValueError(f"No body surfaces remain on {mesh}.")
        gltf.write_text(json.dumps(parsed), encoding="utf-8")
        stock = {key: record for key, record in stock.items() if key in keep_materials}

    images = {p.stem.casefold(): p for p in material_data.rglob("*.png")}
    sources = textures / "source"
    sources.mkdir(parents=True, exist_ok=True)

    # Export every texture the compiled shaders bind, grouped by package.
    exported, groups = {}, {}
    needed = {ref for record in stock.values() for ref in record["refs"].values()}
    for source, object_path, kind in sorted(needed):
        groups.setdefault((source, object_path if kind == "TextureCube" else None), []).append((object_path, kind))
    for number, ((source, cube), targets) in enumerate(groups.items()):
        folder = scratch / f"texture_{number:02}"
        try:
            names = dict.fromkeys(path[-1] for path, _ in targets)
            read_package(tool, root, source, ["-export", "-groups", f"-out={folder.as_posix()}", "-png",
                                             *(f"-obj={name}" for name in names)], scratch)
        except RuntimeError as exc:
            log(f"Warning: could not extract every material texture from {source}. {exc}")
        if cube:
            faces = [next(folder.rglob(f"CubemapFace{i}.png"), None) for i in range(6)]
            if all(faces):
                copies = []
                for i, face in enumerate(faces):
                    copies.append(sources / f"{number:02}_{cube[-1]}_{i}.png")
                    shutil.copyfile(face, copies[-1])
                exported[(source, cube, "TextureCube")] = copies
            continue
        pngs = list(folder.rglob("*.png"))
        for index, (object_path, kind) in enumerate(targets):
            if image := select(pngs, object_path):
                copy = sources / f"{number:02}_{index:02}_{image.name}"
                shutil.copyfile(image, copy)
                exported[(source, object_path, kind)] = copy

    materials, visor_materials = {}, []
    for key, record in stock.items():
        shader_map = record["shader_map"]
        if record["blend"] in TRANSPARENT_BLENDS:
            visor_materials.append(key)
        if shader_map is None:
            # No compiled shader: keep the textures UE Viewer associates with the material.
            fallback = {}
            mat = next((m for m in material_data.rglob("*.mat") if m.stem.casefold() == key), None)
            for line in mat.read_text(encoding="utf-8", errors="replace").splitlines() if mat else ():
                role, sep, image = line.partition("=")
                if sep and role in ("Diffuse", "Normal") and image.casefold() in images:
                    shutil.copyfile(images[image.casefold()], sources / images[image.casefold()].name)
                    fallback["Base Color" if role == "Diffuse" else "Normal"] = f"source/{images[image.casefold()].name}"
            materials[key] = {"files": fallback, "settings": {}, "master": record["master"] or "",
                              "translation": "Unresolved shader: UE Viewer texture assignment"}
            continue
        bindings = {}
        for slot, ref in record["refs"].items():
            if ref not in exported:
                log(f"Warning: UE Viewer did not export {'.'.join(ref[1])} for {key}; it samples black.")
                continue
            if ref[2] == "TextureCube":
                bindings[slot] = {"kind": "TextureCube", "faces": exported[ref]}
            else:
                bindings[slot] = {"kind": "Texture2D", "path": exported[ref], **record["sampling"][slot]}
        log(f"Running the game shaders of {key} ({shader_map.name})")
        baked = bake.Material(game_data.shaders, shader_map, record["scalars"], record["vectors"], bindings, log,
                              game_data.config)
        maps = baked.bake()
        cube = next((baked.textures[name] for name in sorted(baked.textures)
                     if name.startswith("TextureCube_") and baked.textures[name]), None)
        files, settings, notes = bake.write_maps(maps, textures, f"{index_name(key)}", record["blend"], cube)
        for note in notes:
            log(f"Warning: {key}: {note}.")
        materials[key] = {"files": files, "settings": settings, "master": shader_map.name,
                          "translation": "Compiled game shader " + ("(light + base pass)" if baked.light else "(unlit)")}
    return {"gltf": str(gltf), "textures": str(textures), "materials": materials,
            "name": mesh_name, "visor_materials": visor_materials, "morph_bones": bones}


def build(tool, blender, root, game, selection, out, name, render, log, *, identity=(), hide_visor=False,
          preview_only=False, character=None, body_only=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Use 1–64 letters, numbers, hyphens or underscores for the build name.")
    if not selection:
        raise ValueError("Choose at least one mesh.")
    if hide_visor:
        tali = roster(root, game).get("Tali")
        if not tali or tuple(selection) not in tali.suits.values():
            raise ValueError("Hide stock visor requires one of Tali's stock suit presets.")
    render = render or preview_only
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{game}_{name}" + ("_visor_hidden" if hide_visor else "")
    with tempfile.TemporaryDirectory(prefix=".mele-", dir=out) as tmp:
        work = Path(tmp)
        deliverable = work / stem
        deliverable.mkdir()
        parts = []
        game_data = GamePackages(root)
        active_heads = tuple(identity if character else selection)
        profile = roster(root, game)[character] if character else None
        reference_heads = profile.identity if profile else ()
        morphs = {}
        for package, mesh in dict.fromkeys((*active_heads, *reference_heads)):
            companions = tuple(other for source, other in active_heads if source == package)
            if not companions:
                companions = tuple(other for source, other in reference_heads if source == package)
            morph_name = profile.face_morphs.get((package, mesh)) if profile else None
            if morph := game_data.read_morph(package, mesh, character, companions, morph_name=morph_name):
                morphs[(package, mesh)] = morph
        active_morphs = [morphs[item] for item in active_heads if item in morphs]
        if not active_morphs:
            active_morphs = [morphs[item] for item in reference_heads if item in morphs]
        for index, (package, mesh) in enumerate(selection, 1):
            head_morph = morphs.get((package, mesh))
            local_morphs = [value for (source, _), value in morphs.items() if source == package and value in active_morphs]
            matching_hair = [value for value in local_morphs if value.hair == mesh]
            local_morphs = matching_hair or local_morphs
            morph = head_morph or (local_morphs[0] if len(local_morphs) == 1 else None)
            if morph is None and len(active_morphs) == 1 and (package, mesh) not in active_heads:
                morph = active_morphs[0]
            scratch = work / f"part_{index:02}"
            scratch.mkdir()
            source, target = game_data.mesh_asset(package, head_morph.head if head_morph else mesh)
            textures = (work if preview_only else deliverable) / "textures" / f"{index:02}_{target}"
            part = export_mesh(tool, root, source, target, scratch, textures, log, game_data, morph,
                               apply_morph=head_morph is not None,
                               keep_materials=profile.body_materials if body_only and profile else ())
            part["identity"] = (package, mesh) in identity
            part["hide_visor"] = hide_visor
            parts.append(part)
        job = work / "job.json"
        job.write_text(json.dumps({"parts": parts, "out": str(deliverable), "name": stem,
                                   "render": render, "preview_only": preview_only}), encoding="utf-8")
        run_tool([blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python",
                  HERE / "mele_scene.py", "--", job], work, log)
        suffixes = [] if preview_only else [".fbx", ".blend", "_BindPose.fbx"]
        if render:
            suffixes.extend(("_front.png", "_back.png"))
        for suffix in suffixes:
            if not (deliverable / (stem + suffix)).is_file():
                raise RuntimeError(f"Blender did not write {stem}{suffix}.")
        for number in range(1, 10000):
            target = out / (stem if number == 1 else f"{stem}_{number}")
            if target.exists():
                continue
            try:
                return deliverable.rename(target)
            except FileExistsError:
                continue
    raise RuntimeError(f"Too many builds named {stem} in {out}.")
