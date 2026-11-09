# Mass Effect Legendary Edition Armour Builder

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
- The source texture maps under `textures/`.

Pieces with matching bone names and compatible bind transforms share a rig. The .blend attaches a named character’s facial rig to the body when the body has a head bone. The rigged FBX stores incompatible body and facial skeletons as separate rigs. The builder links the diffuse and normal maps that UE Viewer resolves and exports the other recovered texture maps alongside them.

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

Other game shader effects, armour tints, patterns and material variants can differ. Builds currently cover the armour bind pose, with animation, weapon sockets, and in-game loadout assembly outside the implemented workflow.

See [the Anthem workflow](#anthem-armour-builder) below for the original builder.

---

## Anthem Armour Builder

Pick a javelin, an armour set and gear. Get a rigged, textured, animated FBX (and a
ready-to-use `.blend`) for Blender, assembled entirely from Anthem's own game data:

- the armour set's meshes at LOD0, skinned to the game skeleton
- gear mounted on the game's sockets (bone and offset from the javelin's socket file)
- the Forge preview idle, the bind A-pose, and every gear animation, played together with
  the body animation the game pairs it with
- PBR maps (base colour, roughness, metallic, normal) baked from the game textures

| Colossus, Dark Grace (Forge idle) | | |
|---|---|---|
| ![Colossus front](docs/colossus_front.jpg) | ![Colossus back](docs/colossus_back.jpg) | ![Colossus shield up](docs/colossus_shield_aim_side.jpg) |

| Siege shield aim (gear + body clip) | Interceptor, Recon | Ranger, Alien |
|---|---|---|
| ![Shield aim](docs/colossus_shield_aim_front.jpg) | ![Interceptor](docs/interceptor_front.jpg) | ![Ranger](docs/ranger_back.jpg) |

## Requirements

- Windows, with Anthem installed (Origin/EA app, or any folder holding `Anthem.exe`,
  `Data`, `Patch` and `oo2core_7_win64.dll`)
- [Bforartists](https://www.bforartists.de/) or [Blender](https://www.blender.org/)
  (tested with Bforartists 5.2)
- Python 3.10+ (64-bit). The launcher installs Python 3.13 with winget if none is found;
  the builder installs its own packages (numpy, Pillow, texture2ddecoder).

Nothing else needs setting up. On first run the builder finds Anthem and
Bforartists/Blender (it asks once if it can't, and remembers the answer in `config.json`).

## Usage

### Window

Double-click **`Anthem Armour Builder.bat`**.

1. Choose a **javelin**. The first time, the game install is indexed (a few minutes, once),
   and the javelin's files are extracted and catalogued (about a minute per javelin, once).
2. Choose an **armour set**. Optionally mix parts from other sets under *Per-part override*.
3. Tick **gear**. Only one item per socket can be ticked, as in game.
4. Pick a **palette** and output folder, then **Build FBX**.

### Command line

```bash
python anthem_builder.py --list Colossus
python anthem_builder.py --javelin Colossus --set darkgrace --gear wrist_siegeshield --gear shoulder_mortar --gear wrist_sprayer --gear ultimate_gun
```

| Option | |
|---|---|
| `--list [JAVELIN]` | armour sets and gear (all javelins if none given) |
| `--javelin`, `--set` | Colossus / Interceptor / Ranger / Storm, and a set from `--list` |
| `--part part=set` | per-part override, e.g. `--part torso=outlaw` |
| `--gear NAME` | gear to mount (repeat for more) |
| `--palette` | `Dark Grace`, `Gunmetal`, `Ivory` |
| `--out DIR` | output folder (default `../builds`) |
| `--game DIR`, `--bforartists EXE` | override the detected paths |
| `--no-render` | skip the front/back preview renders |

### Output

`<out>/<Javelin>_<set>/`:

- `<Javelin>_<set>.fbx` armature, meshes, embedded textures, every animation as a take
- `<Javelin>_<set>_APose.fbx` static model: the meshes frozen in the A-pose with gear stowed
  (shield collapsed), embedded textures, no armature, bones or skinning
- `<Javelin>_<set>.blend` the same scene, with collections and actions set up
- `<Javelin>_<set>_front.png`, `_back.png` previews
- `maps_<palette>/` the baked texture maps

## In Blender

Open the `.blend` (or import the `.fbx`).

- **Change pose or animation:** select the `<name>_rig` armature, open a *Dope Sheet* in
  *Action Editor* mode and pick an action:
  - `<name>_Forge`: the Forge idle with gear stowed (default)
  - `<name>_APose`: the game's bind A-pose with gear stowed
  - gear clips, e.g. `GCMS_MobileShield_Aim_Idle_FM` or `GMOR_Mortar_Fire`; where the game
    has a matching body clip (`EXH_MobileShield_Aim_Idle_FM`), the body plays it too
- **Collapsed or extended gear:** gear that stows folded (shield, cannons) comes twice, in
  `<gear> collapsed` and `<gear> extended` collections. Hide either with the eye icon in the
  Outliner.

  | A-pose, collapsed shield | A-pose, extended shield |
  |---|---|
  | ![collapsed](docs/colossus_apose_collapsed.jpg) | ![extended](docs/colossus_apose_extended.jpg) |

- **Raw bind pose:** Armature properties, *Pose*, **Rest Position** (gear shows unfolded).
- **Layering:** in the *Nonlinear Animation* editor, stack a body action and a gear-only clip.
- **Posing by hand:** Pose Mode (Ctrl+Tab), select bones, rotate with R; Alt+R / Alt+G to clear.

## How it works

1. **Game files.** The bundled [anthemtool](https://github.com/xyrin88/anthemtool) reads the
   install's layouts, TOCs and superbundles once into `data/game_index.sqlite` (every file's
   CAS archive and offset). After that only the files a javelin needs are decompressed into
   `data/game/`, once each; mesh and texture data follow when a build reads them.
2. **Catalogue** (`catalog.py`, per javelin). Armour sets follow appearance, model and mesh
   EBX references. The skeleton is the rig whose joints fit the frame mesh's skin weights.
   Gear comes from `equipment/gear/<class>/`, with its socket from `<javelin>_sockets.ebx`.
3. **Decoding.** MeshSets (planar vertex streams, half/float positions, 4-bone skinning
   through the render skeleton's bone order), textures (BC1/3/5/7, RGBA8), the ANT
   AssetBank format and Eclipse/Frame animation clips (smallest-three quaternions,
   quantised vectors).
4. **Build** (`build_fbx.py`, inside Bforartists/Blender). One armature: the javelin plus
   each gear rig, whose `AITrajectory` joint (its `RootSocket`) sits on the body socket.
   Meshes are skinned, actions keyed, previews rendered, and the `.blend` and `.fbx` saved.

## Limitations

- Paint uses preset palettes, not each set's real material and colour data.
- Gear and body clips are paired by name and played raw. The game's animation state
  machine (blending, additive layers, IK) is not run, body root motion is dropped, and
  additive and `RawAnimationAsset` clips are skipped. In a paired clip, gear sits on its
  socket, or where the clip authored it while that is on a hand (the Colossus's siege
  cannon); only the gear in use deploys.
- The stowed pose (collapsed idle when there is one) and the extended pose (aim idle) are
  chosen by clip name.
- Gear without its own rigged model in the gear folders (most Interceptor gear, for
  example) is not listed.
- Tested on the Colossus, Interceptor and Ranger; the Storm is catalogued but untested.

## License

MIT, see [LICENSE](LICENSE), which also credits anthemtool (MIT) and the Python
packages used. Anthem and its assets belong to Electronic Arts / BioWare. This
repository contains no game files, and the project is not affiliated with or endorsed
by them.
