
"""Mass Effect Legendary Edition Armour Builder, with a desktop and CLI entry point."""
import argparse
import importlib.util
import queue
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# The material bake runs the game's shaders with numpy and reads textures with Pillow.
_missing = [pip for module, pip in (("numpy", "numpy"), ("PIL", "pillow")) if importlib.util.find_spec(module) is None]
if _missing:
    print(f"Installing Python packages: {', '.join(_missing)} ...", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", *_missing], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    importlib.invalidate_caches()

import mele_assets as assets  # noqa: E402
from mele_characters import CUSTOM_SUIT, MODES, MODE_LABELS, roster  # noqa: E402

DEFAULT_OUT = assets.HERE.parent / "mele-builds"


def build_name(character, mode, look, items):
    if not character:
        return items[0][1].rsplit(".", 1)[-1]
    label = f"{character} {look}"
    if mode == "suit":
        label += " only"
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")


def identity_parts(profile, selection):
    return tuple(part for part in selection
                 if profile.is_identity(part) or re.search(r"_HAT_", part[1], re.I))


def cli(args):
    root = assets.biogame(args.game or assets.game_root(), args.edition)
    if args.characters:
        for name, character in roster(root, args.edition).items():
            print(f"{name}")
            for mode, label in zip(MODES, MODE_LABELS):
                if looks := character.looks(mode):
                    print(f"  {label}: {', '.join(looks)}")
        return
    if args.list == "":
        print("\n".join(assets.armour_packages(root)))
        return
    if args.list:
        print("\n".join(assets.meshes(root, assets.select_package(root, args.list))))
        return
    selection = []
    for item in args.mesh:
        package, sep, mesh = item.partition("::")
        if not sep:
            package, mesh = args.package, item
        if not package or not mesh:
            raise ValueError("Use --mesh PACKAGE::MESH, or specify a default --package.")
        selection.append((assets.select_package(root, package), mesh))
    mode = args.mode or "suit"
    name, look, identity = None, None, ()
    if args.character:
        characters = roster(root, args.edition)
        name = next((key for key in characters if key.casefold() == args.character.casefold()), None)
        if not name:
            raise ValueError(f"Unknown {args.edition} character. Choose from {', '.join(characters)}.")
        profile = characters[name]
        mode = args.mode or ("character" if profile.looks("character") else "character-suit")
        if selection:
            if mode in ("character", "body"):
                raise ValueError("Use --mode character-suit to export a character with custom pieces.")
            if args.suit:
                raise ValueError("Choose a preset with --look or custom pieces with --mesh.")
            look = CUSTOM_SUIT
        else:
            looks = profile.looks(mode)
            if not looks:
                raise ValueError(f"{name} has no separate assets for this export mode.")
            look = next(iter(looks))
            if args.suit:
                look = next((label for label in looks if label.casefold() == args.suit.casefold()), None)
                if look is None:
                    raise ValueError(f"Choose a look from {', '.join(looks)}.")
        selection = profile.selection(mode, look, tuple(selection))
        identity = identity_parts(profile, selection) if mode != "suit" else ()
    elif mode != "suit" or args.suit:
        raise ValueError("Character modes and named looks require --character.")
    selection = list(dict.fromkeys(selection))
    tool = assets.umodel_path(args.umodel, print)
    folder = assets.build(tool, assets.blender_path(args.blender), root, args.edition, selection,
                          args.out, args.name or build_name(name, mode, look, selection),
                          not args.no_render, print, identity=identity, hide_visor=args.hide_visor,
                          character=name, body_only=mode == "body")
    print(f"Saved {folder}")


def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    window = tk.Tk()
    window.title("Mass Effect Legendary Edition Armour Builder")
    window.geometry("1050x660")
    window.minsize(840, 535)
    frame = ttk.Frame(window, padding=16)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(5, weight=1)
    game = tk.StringVar(value=str(assets.game_root()))
    edition = tk.StringVar(value="LE3")
    character = tk.StringVar(value="Shepard")
    mode = tk.StringVar(value=MODE_LABELS[0])
    output = tk.StringVar(value=str(DEFAULT_OUT))
    name = tk.StringVar()
    render = tk.BooleanVar(value=True)
    hide_visor = tk.BooleanVar(value=False)
    view = tk.StringVar(value="front")
    preview_cache = {}
    preview_dir = tempfile.TemporaryDirectory(prefix="mele-previews-")
    list_title = tk.StringVar()
    status = tk.StringVar()
    responses = queue.Queue()
    catalog_root = ""
    characters = {}
    root = None
    busy = False

    def pick_dir(var, title):
        if path := filedialog.askdirectory(title=title):
            var.set(path)
            if var == game:
                refresh()

    def path_row(label, var, index, title):
        ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", pady=4)
        entry = ttk.Entry(frame, textvariable=var)
        entry.grid(row=index, column=1, sticky="ew", padx=8)
        button = ttk.Button(frame, text="Browse…", command=lambda: pick_dir(var, title))
        button.grid(row=index, column=2, sticky="ew")
        return entry, button

    game_entry, game_browse = path_row("Game install", game, 0, "Choose the Legendary Edition install")
    ttk.Label(frame, text="Game").grid(row=1, column=0, sticky="w", pady=4)
    editions = ttk.Combobox(frame, textvariable=edition, values=assets.GAMES, state="readonly", width=8)
    editions.grid(row=1, column=1, padx=8, sticky="w")
    ttk.Label(frame, text="Character").grid(row=2, column=0, sticky="w", pady=4)
    character_box = ttk.Combobox(frame, textvariable=character, state="readonly")
    character_box.grid(row=2, column=1, padx=8, sticky="ew")
    ttk.Label(frame, text="Export").grid(row=3, column=0, sticky="w", pady=4)
    modes = ttk.Combobox(frame, textvariable=mode, state="readonly")
    modes.grid(row=3, column=1, padx=8, sticky="ew")
    ttk.Label(frame, textvariable=list_title).grid(row=4, column=1, padx=8, sticky="w", pady=(12, 3))

    looks_box = tk.Listbox(frame, exportselection=False, activestyle="dotbox", height=8)
    scroll = ttk.Scrollbar(frame, command=looks_box.yview)
    looks_box.configure(yscrollcommand=scroll.set)
    looks_box.grid(row=5, column=1, padx=(8, 0), sticky="nsew")
    scroll.grid(row=5, column=2, sticky="nsw")
    visor_box = ttk.Checkbutton(frame, text="Hide stock visor", variable=hide_visor,
                                command=lambda: update_controls())
    visor_box.grid(row=6, column=1, padx=8, pady=3, sticky="w")
    path_row("Output folder", output, 7, "Choose the output folder")
    ttk.Label(frame, text="Build name").grid(row=8, column=0, sticky="w", pady=4)
    ttk.Entry(frame, textvariable=name).grid(row=8, column=1, padx=8, sticky="ew")
    ttk.Checkbutton(frame, text="Render previews", variable=render).grid(row=8, column=2, sticky="w")
    ttk.Label(frame, textvariable=status, wraplength=450).grid(row=9, column=0, columnspan=2,
                                                             sticky="w", pady=(12, 0))
    build_button = ttk.Button(frame, text="Build FBX + Blend", command=lambda: build())
    build_button.grid(row=9, column=2, sticky="ew", pady=(12, 0))

    panel = ttk.LabelFrame(frame, text="Preview", padding=8, width=332)
    panel.grid(row=0, column=3, rowspan=10, padx=(16, 0), sticky="nsew")
    panel.grid_propagate(False)
    panel.columnconfigure(0, weight=1)
    panel.rowconfigure(1, weight=1)
    preview_button = ttk.Button(panel, text="Render selected look", command=lambda: build(preview=True))
    preview_button.grid(row=0, column=0, sticky="ew")
    preview_image = ttk.Label(panel, anchor="center", justify="center", wraplength=265)
    preview_image.grid(row=1, column=0, sticky="nsew")
    angles = ttk.Frame(panel)
    angles.grid(row=2, column=0)
    for direction in ("front", "back"):
        ttk.Radiobutton(angles, text=direction.title(), variable=view, value=direction,
                        command=lambda: show_preview()).pack(side="left", padx=6)

    def selection_key():
        if root is None or not looks_box.curselection():
            return None
        profile = characters[character.get()]
        kind = MODES[MODE_LABELS.index(mode.get())]
        look = looks_box.get(looks_box.curselection()[0])
        return (root, edition.get(), profile.selection(kind, look), hide_visor.get())

    def show_preview():
        folder = preview_cache.get(selection_key())
        path = next(folder.glob(f"*_{view.get()}.png"), None) if folder else None
        if path:
            photo = tk.PhotoImage(master=window, file=str(path))
            factor = max((photo.width() + 299) // 300, (photo.height() + 389) // 390)
            preview_image.image = photo.subsample(factor)
            preview_image.configure(image=preview_image.image, text="")
        else:
            preview_image.configure(image="", text="Select a look and click Render selected look.")
            preview_image.image = None

    def update_controls():
        profile = characters.get(character.get())
        ready = not busy and profile is not None
        for widget in (game_entry, game_browse):
            widget.configure(state="disabled" if busy else "normal")
        for widget in (editions, character_box, modes):
            widget.configure(state="disabled" if busy else "readonly")
        looks_box.configure(state="disabled" if busy else "normal")
        action_state = "normal" if ready and looks_box.curselection() else "disabled"
        build_button.configure(state=action_state)
        preview_button.configure(state=action_state)
        if ready and character.get() == "Tali":
            visor_box.grid()
        else:
            visor_box.grid_remove()
            if not busy:
                hide_visor.set(False)
        show_preview()

    def update_looks(*_):
        profile = characters.get(character.get())
        kind = MODES[MODE_LABELS.index(mode.get())] if profile else None
        labels = list(profile.looks(kind)) if profile else []
        previous = looks_box.get(looks_box.curselection()[0]) if looks_box.curselection() else ""
        looks_box.delete(0, "end")
        if labels:
            looks_box.insert("end", *labels)
            index = labels.index(previous) if previous in labels else 0
            looks_box.selection_set(index)
            looks_box.activate(index)
            looks_box.see(index)
        list_title.set({"character": "Unclothed looks", "body": "Base body"}.get(kind, "Outfits"))
        if profile:
            status.set("Tali's face and suit share one mesh." if character.get() == "Tali" else "")
        update_controls()

    def update_character(*_):
        profile = characters.get(character.get())
        options = [label for kind, label in zip(MODES, MODE_LABELS) if profile and profile.looks(kind)]
        modes.configure(values=options)
        if mode.get() not in options:
            mode.set(options[0] if options else "")
        update_looks()

    def refresh(*_):
        nonlocal catalog_root, characters, root
        catalog_root = game.get()
        try:
            root = assets.biogame(Path(catalog_root), edition.get())
            characters = roster(root, edition.get())
        except (ValueError, OSError) as exc:
            root, characters = None, {}
            status.set(str(exc))
        else:
            if not characters:
                status.set("No installed character presets were found for this game.")
        character_box.configure(values=list(characters))
        if character.get() not in characters:
            character.set(next(iter(characters), ""))
        update_character()

    def poll():
        nonlocal busy
        while not responses.empty():
            kind, value = responses.get_nowait()
            if kind == "log":
                status.set(value)
                continue
            busy = False
            update_controls()
            if kind == "error":
                status.set("The operation failed.")
                messagebox.showerror("Armour Builder", value)
            else:
                folder, key, preview, previews = value
                if previews:
                    preview_cache[key] = folder
                    show_preview()
                status.set("Preview ready." if preview else f"Saved {folder}")
            return
        window.after(80, poll)

    def build(preview=False):
        nonlocal busy
        if game.get() != catalog_root:
            refresh()
            return
        if busy or not looks_box.curselection() or root is None:
            return
        who, title = character.get(), edition.get()
        profile = characters[who]
        kind = MODES[MODE_LABELS.index(mode.get())]
        look = looks_box.get(looks_box.curselection()[0])
        items = profile.selection(kind, look)
        key = selection_key()
        if preview and key in preview_cache:
            show_preview()
            status.set("Preview ready.")
            return
        label = "Preview" if preview else (name.get() or build_name(who, kind, look, items))
        out = Path(preview_dir.name) if preview else Path(output.get())
        previews, hidden_visor = preview or render.get(), hide_visor.get()
        try:
            blender = assets.blender_path(None)
        except ValueError:
            chosen = filedialog.askopenfilename(title="Choose Blender or Bforartists",
                                                filetypes=[("Windows application", "*.exe")])
            if not chosen:
                return
            blender = Path(chosen)
        identity = identity_parts(profile, items) if kind != "suit" else ()

        def worker():
            log = lambda line: responses.put(("log", line))
            try:
                folder = assets.build(assets.umodel_path(None, log), blender, root, title, items,
                                      out, label, previews, log, identity=identity,
                                      hide_visor=hidden_visor, preview_only=preview, character=who,
                                      body_only=kind == "body")
            except Exception as exc:
                responses.put(("error", str(exc)))
            else:
                responses.put(("done", (folder, key, preview, previews)))

        busy = True
        status.set("Rendering preview…" if preview else "Building…")
        update_controls()
        threading.Thread(target=worker, daemon=True).start()
        window.after(80, poll)

    editions.bind("<<ComboboxSelected>>", refresh)
    character_box.bind("<<ComboboxSelected>>", update_character)
    modes.bind("<<ComboboxSelected>>", update_looks)
    game_entry.bind("<Return>", refresh)
    looks_box.bind("<<ListboxSelect>>", lambda _: update_controls())
    window.protocol("WM_DELETE_WINDOW", lambda: None if busy else window.destroy())
    refresh()
    try:
        window.mainloop()
    finally:
        preview_dir.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edition", choices=assets.GAMES, default="LE3")
    parser.add_argument("--game", type=Path, help="Legendary Edition install directory")
    parser.add_argument("--list", nargs="?", const="", metavar="PACKAGE", help="list armour packages or a package's meshes")
    parser.add_argument("--characters", action="store_true", help="list named characters and suits")
    parser.add_argument("--character", help="character name from --characters")
    parser.add_argument("--mode", choices=MODES, help="character, character plus suit, suit only, or base body only")
    parser.add_argument("--look", "--suit", dest="suit", help="named look for the export mode from --characters")
    parser.add_argument("--package", help="default BioGame-relative package path for --mesh")
    parser.add_argument("--mesh", action="append", default=[], help="mesh name or PACKAGE::MESH (repeatable)")
    parser.add_argument("--name", help="output build name")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--umodel", help="override the UE Viewer executable")
    parser.add_argument("--blender", help="override the Blender or Bforartists executable")
    parser.add_argument("--hide-visor", action="store_true", help="hide the visor surface on a stock Tali preset")
    parser.add_argument("--no-render", action="store_true", help="skip front and back previews")
    if len(sys.argv) == 1:
        gui()
        return 0
    args = parser.parse_args()
    if args.list is None and not (args.characters or args.character or args.mesh):
        parser.error("provide --characters, --character, --list or at least one --mesh")
    try:
        cli(args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
