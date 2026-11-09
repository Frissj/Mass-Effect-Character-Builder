# Mass Effect Character Builder

Build rigged characters and armour for Blender from your local Mass Effect Legendary Edition install. The named picker includes default Shepard, FemShep, squadmates and crew from LE1, LE2 and LE3.

## Start the builder

1. Double-click **Mass Effect Armour Builder.bat**. The builder finds the Steam install and Bforartists or Blender. You can browse to either if detection fails.
2. Choose the game, character and export mode. The list shows one row per look.
3. Select a look and click **Render selected look** for front and back views in the preview pane. Then click **Build FBX + Blend** to export. The first preview or build downloads and verifies [UE Viewer](https://www.gildor.org/en/projects/umodel) build 1590. It reads the installed .pcc packages and texture caches directly.

- **Suit only** lists clothing, uniforms, formalwear and armour without the character where the meshes are separable. A modular suit automatically includes its torso, arms, shoulders and legs.
- **Character + suit** lists the same outfits with the matching character assets. A closed-helmet outfit can already include the head in its full-body geometry.
- **Character only** lists the base body with the head where available, and can export the head and hair on their own.
- **Body mesh only** appears for characters with a standalone base body. The base mesh can include built-in underwear.

Tali's stock suit and face share one mesh. Her picker offers the suit modes and **Hide stock visor**, which removes the suit's translucent surfaces (the visor, identified by its material's blend mode) and keeps the original geometry behind it. Some squadmates only have a head or head-and-hair preset in Character only. The builder applies a stock face morph when the selected head or its companion hair identifies it. If a package contains multiple unresolved morphs for the same head, the builder uses the stock base mesh. The catalogue covers selected stock looks. Raw package and mesh selection is available on the command line.

Windows, Python 3.10 or later, and Blender 4.2 or later or an equivalent Bforartists build are required. The launcher offers to install Python 3.13 if it is missing.

## Command line

```powershell
python mele_builder.py --edition LE1 --characters
python mele_builder.py --edition LE1 --character Shepard --mode character
python mele_builder.py --edition LE3 --character Shepard --mode body
python mele_builder.py --edition LE2 --character FemShep --mode character-suit
python mele_builder.py --edition LE3 --character Shepard --mode character-suit --look "N7 full body (FBDa)"
python mele_builder.py --edition LE3 --character Traynor --mode character-suit --look "Normandy uniform"
python mele_builder.py --edition LE3 --character Garrus --mode suit
python mele_builder.py --edition LE3 --character Tali --hide-visor
```

List and export specific assets for custom builds.

```powershell
python mele_builder.py --edition LE2 --list
python mele_builder.py --edition LE2 --list CookedPCConsole/BIOG_HMM_ARM_SHP_R.pcc
python mele_builder.py --edition LE1 --package CookedPCConsole/BIOG_HMM_ARM_LGT_R.pcc --mesh HMM_ARM_LGTa_MDL
```

Use `--characters` to see the named looks for each mode. `--look` selects one of those names. Use `--mesh PACKAGE::MESH` to select pieces from different packages. When a package contains meshes with the same name, `--list PACKAGE` prints their qualified object names for use with `--mesh`. Add `--character NAME --mode character-suit` to include the named character's head and accessories with the custom pieces. Package paths are relative to the selected game's `BioGame` directory. Use the full relative path when the same package name appears in the base game and a mod.

Use `--game DIR` or the `MELE_GAME` environment variable to override install detection. `--blender EXE`, `--umodel EXE`, `--out DIR` and `--no-render` override the executable paths, output folder and preview rendering.

## Output

Each build creates a folder under `../mele-builds` by default.

- A rigged LOD0 `.fbx` with embedded textures and a `.blend` with packed images.
- A static `_BindPose.fbx` and optional front and back PNG previews.
- The baked material maps under `textures/`, and the game textures the shaders sampled under `textures/source/`.

Pieces with matching bone names and compatible bind transforms share a rig. The .blend attaches a named character’s facial rig to the body when the body has a head bone. The rigged FBX stores incompatible body and facial skeletons as separate rigs.

## Materials

Materials come from the game's own compiled shaders. The cooked packages strip each material's node graph, so the builder reads the shaders from `RefShaderCache-PC-D3D-SM5.upk` instead. It follows each material instance to the permutation the engine renders, which is set by the instance's static parameters or by the master's default. It then evaluates the uniform expressions with the instance and stock face-morph parameters and binds the textures those shaders sample. Finally it runs the base pass and directional-light pixel shaders over the texture grid with a small DXBC interpreter (`mele_dxbc.py`, which uses Windows' `d3dcompiler_47.dll` to disassemble). No material is translated by hand.

The light shader is probed with controlled light and view directions around each texel's normal. From the responses the builder bakes:

- the base colour (the response at normal incidence),
- the base pass's normal output,
- up to two specular lobes (fitted on a geometric R·L sweep and mapped to Principled specular and coat),
- the response to light from behind (a translucent lobe),
- emission, opacity (masked or translucent),
- and the material's own cube-map reflection weight.

Unlit shaders, such as lashes and visors, become emission. Each Blender material records its master and translation in custom properties.

BioWare's materials use custom lighting, and a Principled BSDF can only approximate that. View-dependent terms are sampled at the cosine-weighted mean view angle, Phong lobes are converted to GGX roughness, and the cube reflection is added as emission. The FBX files carry the standard inputs; the translucent and reflection graphs are saved in the .blend. Shader inputs are in `textures/source` and the baked maps are in `textures`. Materials that read a second UV set are baked in the first one, and the build log says so.

BioWare's runtime effect layers (damage, biotics, holograms) are left off, and animated shader terms are baked at time zero. Builds cover the bind pose; animation, weapon sockets and in-game loadout assembly are outside the implemented workflow.

## License

MIT, see [LICENSE](LICENSE). Mass Effect and its assets belong to Electronic Arts / BioWare. This repository contains no game files, and the builder only reads a copy of the game you own.

