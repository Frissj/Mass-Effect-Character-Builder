
"""Run inside Blender to assemble and export the selected Legendary Edition meshes."""
import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Quaternion, Vector


def studio_render(scene, out_dir, name, framing, percentage=100):
    """Render front and back previews with a three-point studio rig and an orthographic camera."""
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    background = next(node for node in world.node_tree.nodes if node.type == "BACKGROUND")
    background.inputs["Color"].default_value = (0.32, 0.36, 0.42, 1)
    background.inputs["Strength"].default_value = 0.6

    def light(label, energy, location, rotation, size, colour=(1, 1, 1)):
        data = bpy.data.lights.new(label, "AREA")
        data.energy, data.size, data.color = energy, size, colour
        obj = bpy.data.objects.new(label, data)
        obj.location, obj.rotation_euler = location, [math.radians(a) for a in rotation]
        scene.collection.objects.link(obj)
        return obj

    lights = [light("Key", 900, (2.5, -3.5, 4.0), (50, 0, 35), 3.0, (1, 0.96, 0.9)),
              light("Fill", 350, (-3.5, -2.5, 2.5), (65, 0, -55), 4.0, (0.85, 0.9, 1)),
              light("Rim", 700, (0, 3.5, 3.5), (-50, 0, 180), 3.0)]
    camera = bpy.data.objects.new("PreviewCamera", bpy.data.cameras.new("PreviewCamera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    scene.render.engine = "BLENDER_EEVEE" if "BLENDER_EEVEE" in engines else "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x, scene.render.resolution_y = 900, 1100
    scene.render.resolution_percentage = percentage
    scene.view_settings.view_transform = "AgX"
    (x, y, z), scale = framing
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = scale
    for view, location, yaw in (("front", (x, y - 5, z), 0), ("back", (x, y + 5, z), 180)):
        camera.location, camera.rotation_euler = location, (math.radians(90), 0, math.radians(yaw))
        scene.render.filepath = str(Path(out_dir) / f"{name}_{view}.png")
        bpy.ops.render.render(write_still=True)
    # Keep the exported files to the character only.
    for obj in lights + [camera]:
        bpy.data.objects.remove(obj)


def bind_normal(tree, shader, texture, socket="Normal"):
    normal = tree.nodes.new("ShaderNodeNormalMap")
    normal.location = (-260, -100)
    tree.links.new(texture.outputs["Color"], normal.inputs["Color"])
    tree.links.new(normal.outputs["Normal"], shader.inputs[socket])


COLOUR_MAPS = ("Base Color", "Emission Color", "Reflection Cube")


def image_node(tree, folder, filename, role, row):
    environment = role == "Reflection Cube"
    node = tree.nodes.new("ShaderNodeTexEnvironment" if environment else "ShaderNodeTexImage")
    node.image = bpy.data.images.load(str(folder / filename), check_existing=True)
    if role not in COLOUR_MAPS:
        node.image.colorspace_settings.name = "Non-Color"
    node.image.alpha_mode = "CHANNEL_PACKED" if role != "Base Color" else "STRAIGHT"
    node.label = role
    node.location = (-700, 300 - row * 270)
    return node


def bind_material(tree, shader, folder, files, settings):
    """Connect the maps baked from the material's compiled game shaders."""
    nodes = {role: image_node(tree, folder, filename, role, row) for row, (role, filename) in enumerate(files.items())}
    for role, node in nodes.items():
        if role in ("Normal", "Coat Normal"):
            bind_normal(tree, shader, node, role)
        elif role in shader.inputs and role != "Emission Color":
            tree.links.new(node.outputs["Color"], shader.inputs[role])
    for socket in ("IOR",):
        if socket in settings:
            shader.inputs[socket].default_value = settings[socket]
    if settings.get("alpha") and "Base Color" in nodes:
        tree.links.new(nodes["Base Color"].outputs["Alpha"], shader.inputs["Alpha"])
    if settings.get("unlit"):
        # No light pass: the game draws the base pass colour as is.
        shader.inputs["Base Color"].default_value = (0, 0, 0, 1)
        for link in list(shader.inputs["Base Color"].links):
            tree.links.remove(link)
        shader.inputs["Specular IOR Level"].default_value = 0

    def scaled(colour_socket, factor):
        node = tree.nodes.new("ShaderNodeVectorMath")
        node.operation = "SCALE"
        tree.links.new(colour_socket, node.inputs[0])
        node.inputs["Scale"].default_value = factor
        return node.outputs["Vector"]

    emissive = []
    if "Emission Color" in nodes:
        emissive.append(scaled(nodes["Emission Color"].outputs["Color"], settings.get("Emission Strength", 1)))
    if "Reflection Cube" in nodes:
        # The material's own cube-map reflection, weighted per texel as the game shader does.
        coords = tree.nodes.new("ShaderNodeTexCoord")
        tree.links.new(coords.outputs["Reflection"], nodes["Reflection Cube"].inputs["Vector"])
        product = tree.nodes.new("ShaderNodeVectorMath")
        product.operation = "MULTIPLY"
        tree.links.new(nodes["Reflection Cube"].outputs["Color"], product.inputs[0])
        tree.links.new(nodes["Reflection Weight"].outputs["Color"], product.inputs[1])
        emissive.append(scaled(product.outputs["Vector"], settings.get("Reflection Strength", 1)))
    if emissive:
        total = emissive[0]
        for other in emissive[1:]:
            add = tree.nodes.new("ShaderNodeVectorMath")
            add.operation = "ADD"
            tree.links.new(total, add.inputs[0])
            tree.links.new(other, add.inputs[1])
            total = add.outputs["Vector"]
        tree.links.new(total, shader.inputs["Emission Color"])
        shader.inputs["Emission Strength"].default_value = 1
    if "Transmission" in nodes:
        # The light pass's response to light from behind the surface.
        translucent = tree.nodes.new("ShaderNodeBsdfTranslucent")
        tree.links.new(nodes["Transmission"].outputs["Color"], translucent.inputs["Color"])
        if shader.inputs["Normal"].is_linked:
            tree.links.new(shader.inputs["Normal"].links[0].from_socket, translucent.inputs["Normal"])
        add = tree.nodes.new("ShaderNodeAddShader")
        add.label = "LE3 light wrap"
        tree.links.new(shader.outputs[0], add.inputs[0])
        tree.links.new(translucent.outputs[0], add.inputs[1])
        output = next(node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
        tree.links.new(add.outputs[0], output.inputs["Surface"])


def bind_materials(mesh, part):
    folder = Path(part["textures"])
    for material in dict.fromkeys(mesh.data.materials):
        if material is None:
            continue
        name = material.name.casefold()
        key = name if name in part["materials"] else name.rsplit(".", 1)[0]
        record = part["materials"].get(key)
        if record is None:
            continue
        tree = material.node_tree
        shader = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
        for link in list(tree.links):
            if link.to_node == shader:
                tree.links.remove(link)
        shader.inputs["Metallic"].default_value = 0
        material["LE material master"] = record["master"]
        material["LE material translation"] = record["translation"]
        bind_material(tree, shader, folder, record["files"], record["settings"])


def remove_stock_visor(mesh, materials):
    slots = {i for i, material in enumerate(mesh.materials)
             if material and material.name.casefold() in materials}
    if not slots:
        return False
    edit = bmesh.new()
    try:
        edit.from_mesh(mesh)
        faces = [face for face in edit.faces if face.material_index in slots]
        if not faces or len(faces) == len(edit.faces):
            return False
        bmesh.ops.delete(edit, geom=faces, context="FACES")
        edit.to_mesh(mesh)
        mesh.update()
    finally:
        edit.free()
    return True


def apply_face_bones(rig, part):
    positions = {name.casefold(): value for name, value in part["morph_bones"].items()}
    if not positions:
        return
    doc = json.loads(Path(part["gltf"]).read_text(encoding="utf-8"))
    nodes = doc["nodes"]
    parents = {child: index for index, node in enumerate(nodes) for child in node.get("children", [])}
    matrices = {}

    def world(index):
        if index not in matrices:
            node = nodes[index]
            x, y, z, w = node.get("rotation", (0, 0, 0, 1))
            rotation = Quaternion((w, x, y, z))
            base = node.get("translation", (0, 0, 0))
            name = node.get("name", "").casefold()
            original = Matrix.LocRotScale(Vector(base), rotation, None)
            morphed = Matrix.LocRotScale(Vector(positions.get(name, base)), rotation, None)
            if index in parents:
                old_parent, new_parent = world(parents[index])
                original, morphed = old_parent @ original, new_parent @ morphed
            matrices[index] = original, morphed
        return matrices[index]

    deltas, matched = {}, set()
    to_armature = rig.matrix_world.inverted().to_3x3()
    for index in doc["skins"][0]["joints"]:
        name = nodes[index]["name"]
        if name.casefold() in positions:
            matched.add(name.casefold())
        base, shaped = world(index)
        x, y, z = shaped.translation - base.translation
        deltas[name] = to_armature @ Vector((x, -z, y))
    if matched != positions.keys():
        raise ValueError(f"The face morph bones do not match the stock head rig for {part['name']}.")

    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for name, delta in deltas.items():
            bone = rig.data.edit_bones[name]
            bone.head += delta
            bone.tail += delta
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


def import_part(part):
    before = set(bpy.context.scene.objects)
    # Keep native bone orientations consistent across meshes with the same skeleton.
    bpy.ops.import_scene.gltf(filepath=part["gltf"], bone_heuristic="TEMPERANCE")
    imported = set(bpy.context.scene.objects) - before
    rigs = [obj for obj in imported if obj.type == "ARMATURE"]
    meshes = [obj for obj in imported if obj.type == "MESH"]
    if len(rigs) != 1 or not meshes or any(not obj.vertex_groups for obj in meshes):
        raise ValueError(f"{part['name']} did not import as a skinned mesh.")
    if part["hide_visor"]:
        removed = [remove_stock_visor(obj.data, part["visor_materials"]) for obj in meshes]
        if not any(removed):
            raise ValueError(f"No separate visor surface was found on {part['name']}.")
    for obj in meshes:
        bind_materials(obj, part)
    rig = rigs[0]
    apply_face_bones(rig, part)
    rig.matrix_world = Matrix.Rotation(-math.pi / 2, 4, "Z") @ rig.matrix_world
    rig.data.display_type = "STICK"
    bpy.context.view_layer.update()
    return rig, meshes, part["identity"]


def same_bind(rig, other, meshes):
    used = {obj.vertex_groups[weight.group].name for obj in meshes
            for vertex in obj.data.vertices for weight in vertex.groups if weight.weight > 0}
    for name in used:
        source = other.data.bones.get(name)
        target = rig.data.bones.get(name)
        if not source or not target:
            return False
        if (source.parent and source.parent.name) != (target.parent and target.parent.name):
            return False
        a_pos, a_rot, a_scale = (other.matrix_world @ source.matrix_local).decompose()
        b_pos, b_rot, b_scale = (rig.matrix_world @ target.matrix_local).decompose()
        # Cooked variants of the same rig can differ by a millimetre or two.
        if (a_pos - b_pos).length > 0.002 or abs(a_rot.dot(b_rot)) < math.cos(math.radians(0.1)):
            return False
        if (a_scale - b_scale).length > 0.001:
            return False
    return True


def consolidate(parts, name):
    parts.sort(key=lambda part: (part[2], -len(part[0].data.bones)))
    kept = []
    for rig, meshes, identity in parts:
        target = next((candidate for candidate in kept if same_bind(candidate, rig, meshes)), None)
        if target is None:
            rig.name = f"{name}_rig" if not kept else f"{name}_rig_{len(kept) + 1}"
            if identity and kept and (head := kept[0].data.bones.get("Head")):
                # Facial bones stay on their own rig and follow the body's head.
                world = rig.matrix_world.copy()
                rig.parent = kept[0]
                rig.parent_type = "BONE"
                rig.parent_bone = head.name
                bpy.context.view_layer.update()
                rig.matrix_world = world
                bpy.context.view_layer.update()
            kept.append(rig)
            continue
        for obj in meshes:
            world = obj.matrix_world.copy()
            obj.parent = target
            obj.matrix_world = world
            for modifier in obj.modifiers:
                if modifier.type == "ARMATURE" and modifier.object == rig:
                    modifier.object = target
        bpy.data.objects.remove(rig, do_unlink=True)
    if len(kept) > 1:
        print(f"Selected meshes use {len(kept)} different bind skeletons. They were kept as separate rigs.")
    return kept


def export_fbx(path, static=False):
    bpy.ops.export_scene.fbx(filepath=str(path), object_types={"MESH"} if static else {"ARMATURE", "MESH"},
                             use_mesh_modifiers=True, add_leaf_bones=False, bake_anim=False,
                             path_mode="COPY", embed_textures=True)


def freeze_meshes(meshes):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    frozen = []
    for obj in meshes:
        evaluated = obj.evaluated_get(depsgraph)
        data = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
        frozen.append((obj.name, data, evaluated.matrix_world.copy()))
    for obj in list(bpy.context.scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for name, data, world in frozen:
        obj = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(obj)
        obj.matrix_world = world


def main():
    job = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
    out = Path(job["out"])
    name = job["name"]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    parts = [import_part(part) for part in job["parts"]]
    rigs = consolidate(parts, name)
    bpy.context.view_layer.update()
    meshes = [obj for _, group, _ in parts for obj in group]
    if job["render"]:
        corners = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
        bounds = [(min(p[i] for p in corners), max(p[i] for p in corners)) for i in range(3)]
        center = tuple((lo + hi) / 2 for lo, hi in bounds)
        scale = max(bounds[2][1] - bounds[2][0], (bounds[0][1] - bounds[0][0]) * 1100 / 900)
        studio_render(bpy.context.scene, out, name, framing=(center, max(0.1, scale * 1.12)),
                      percentage=60 if job["preview_only"] else 100)
    if job["preview_only"]:
        return
    bpy.ops.object.select_all(action="DESELECT")
    rigs[0].select_set(True)
    bpy.context.view_layer.objects.active = rigs[0]
    bpy.context.scene.render.filepath = "//"
    # Make the packed scene independent of the temporary build directory.
    bpy.ops.file.pack_all()
    for img in bpy.data.images:
        if img.packed_file:
            img.filepath = "//" + Path(img.filepath).relative_to(out).as_posix()
    bpy.ops.wm.save_as_mainfile(filepath=str(out / f"{name}.blend"), relative_remap=False)
    # The FBX exporter needs a direct Principled-to-output link. The packed
    # Blender scene above retains the wrap lobe and the wrap map stays on disk.
    for material in bpy.data.materials:
        if material.node_tree:
            for node in material.node_tree.nodes:
                if node.type == "ADD_SHADER" and node.label == "LE3 light wrap":
                    links = material.node_tree.links
                    links.new(node.inputs[0].links[0].from_socket, node.outputs[0].links[0].to_socket)
    # Keep armatures at the FBX root. Blender's FBX importer cannot round-trip
    # a skinned armature parented to a different armature's bone.
    for rig in rigs:
        if rig.parent_type == "BONE" and rig.parent:
            world = rig.matrix_world.copy()
            rig.parent = None
            rig.matrix_world = world
    bpy.context.view_layer.update()
    export_fbx(out / f"{name}.fbx")
    freeze_meshes(meshes)
    export_fbx(out / f"{name}_BindPose.fbx", static=True)


if __name__ == "__main__":
    main()
