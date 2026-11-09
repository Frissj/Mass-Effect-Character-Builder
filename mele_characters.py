
"""Built-in character and suit selections from the retail Legendary Edition packages."""
import re
from dataclasses import dataclass, field, replace

Mesh = tuple[str, str]
MODES = ("character", "character-suit", "suit", "body")
MODE_LABELS = ("Character only", "Character + suit", "Suit only", "Body mesh only")
CUSTOM_SUIT = "Custom mesh selection"


@dataclass(frozen=True)
class Character:
    identity: tuple[Mesh, ...]
    suits: dict[str, tuple[Mesh, ...]]
    body: tuple[Mesh, ...] = ()
    civilian: dict[str, tuple[Mesh, ...]] = field(default_factory=dict)
    dressed: dict[str, tuple[Mesh, ...]] = field(default_factory=dict)
    body_look: tuple[Mesh, ...] = ()
    body_materials: tuple[str, ...] = ()
    head_variants: dict[str, tuple[Mesh, ...]] = field(default_factory=dict)
    face_morphs: dict[Mesh, str] = field(default_factory=dict)

    def is_identity(self, part):
        return (part in self.identity or any(part in meshes for meshes in self.head_variants.values())
                or re.search(r"_(HED|HIR|HEAD|HAIR)_", part[1], re.I) is not None)

    def looks(self, mode):
        if mode == "body":
            return {"Base body": self.body} if self.body else {}
        if mode == "character":
            looks = {}
            if self.body:
                looks["Base body + head"] = self.body_look or (*self.body, *self.identity)
            if self.identity:
                label = "Head + accessories" if len(self.identity) > 1 else "Head only"
                looks[label] = self.identity
            return {**looks, **self.head_variants}
        if mode == "character-suit":
            armour_looks = {label: self.dressed.get(label, (*outfit, *self.identity))
                            for label, outfit in self.suits.items()}
            return {**armour_looks, **self.civilian}
        clothing = {}
        for label, outfit in self.civilian.items():
            meshes = tuple(part for part in outfit if not self.is_identity(part) or "_hat_" in part[1].casefold())
            if meshes and meshes not in clothing.values():
                clothing[label] = meshes
        return {**self.suits, **clothing}

    def selection(self, mode, look=None, custom=()):
        if custom:
            if mode == "character-suit" and not self.identity:
                raise ValueError("This character has no separate head mesh. Choose a named suit.")
            return (*custom, *self.identity) if mode == "character-suit" else custom
        looks = self.looks(mode)
        if not looks:
            raise ValueError("There are no separate assets for this export mode.")
        return looks[look or next(iter(looks))]


def parts(package, *names, dlc=None):
    prefix = f"DLC/{dlc}/" if dlc else ""
    return tuple((f"{prefix}CookedPCConsole/{package}.pcc", name) for name in names)


def armour(prefix, package, *names):
    return parts(f"BIOG_{prefix}_ARM_{package}_R", *names)


CITADEL = "DLC_EXP_Pack003_Base"


def squad(code, body, *identity, alt=None, dlc=None, extras=(), civilian=None):
    package = f"BioH_{code}_00"
    suits = {"Default suit": parts(package, body, dlc=dlc)}
    dressed = {}
    if alt:
        suits["Alternate suit"] = parts(f"BioH_{code}_01", alt, dlc=dlc)
    for label, slot, mesh, dlc_name, *head in extras:
        variant = f"BioH_{code}_{slot}"
        suits[label] = parts(variant, mesh, dlc=dlc_name)
        if head:
            dressed[label] = parts(variant, mesh, *head, dlc=dlc_name)
    return Character(parts(package, *identity, dlc=dlc), suits,
                     civilian=civilian or {}, dressed=dressed)


def le1_armour(prefix):
    """LE1's shared armour meshes. Shepard and the squad wear the set for their body type."""
    suits = {}
    for label, code, variants in (("Light", "LGT", "abc" if prefix == "HMF" else "ab"),
                                  ("Medium", "MED", "abc"), ("Heavy", "HVY", "ab")):
        for letter in variants:
            title = f"{label} armour" + (f" {letter.upper()}" if letter != "a" else "")
            mesh = f"{prefix}_ARM_{code}{letter}_MDL"
            if prefix == "HMF" and code == "LGT" and letter == "c":
                mesh = f"LGTc.{mesh}"
            suits[title] = armour(prefix, code, mesh)
    return suits


def shepard(game, female=False):
    prefix = "HMF" if female else "HMM"
    head_package = f"BIOG_{prefix}_HED_PROMorph" + ("_R" if female else "")
    surname = "Shepard" if female and game == "LE3" else "Sheppard"
    identity = parts(head_package, f"{prefix}_HED_PRO{surname}_MDL")
    if female:
        identity += parts("BIOG_HMF_HIR_PRO", "HMF_HIR_PROShepard_MDL")
    suits, dressed = {}, {}
    if game == "LE1":
        suits.update(le1_armour(prefix))
    else:
        for letter in ("abcd" if game == "LE2" else "abcdefgh"):
            label = "N7 modular (SHPa)" if letter == "a" else f"Modular suit {letter.upper()} (SHP{letter})"
            suits[label] = armour(prefix, "SHP", *(
                f"{prefix}_ARM_SHP{letter}_{slot}_MDL" for slot in ("Tor", "Leg", "Slv", "Sld")))
    if game == "LE2":
        suits["Cerberus modular"] = parts(f"BIOG_{prefix}_ARM_SHP_CER02_R", *(
            f"{prefix}_ARM_SHPe_{slot}_MDL" for slot in ("Tor", "Leg", "Slv", "SLD")), dlc="DLC_CER_02")
        for label, pack, mesh, dlc in (
            ("Blood Dragon armour", "SHP_DA", "SHP_FBDa", "DLC_PRE_DA"),
            ("Cerberus Assault armour", "CBS", "CBS", "DLC_PRE_Cerberus"),
            ("Collector armour", "COL", "COL" if female else "FBDe", "DLC_PRE_Collectors"),
            ("Inferno armour", "INF", "INF", "DLC_PRE_General"),
            ("Terminus armour", "TRM", "TRM", "DLC_PRE_Terminus"),
        ):
            suits[label] = parts(f"BIOG_{prefix}_ARM_{pack}_R", f"{prefix}_ARM_{mesh}_MDL", dlc=dlc)
            dressed[label] = suits[label]  # The full-body mesh already includes the closed helmet.
    if game == "LE3":
        for letter in "abcdefgh":
            label = "N7 full body (FBDa)" if letter == "a" else f"Full-body suit {letter.upper()} (FBD{letter})"
            suits[label] = armour(prefix, "SHP", f"{prefix}_ARM_FBD{letter}_MDL")
            if letter in "bcdefh":
                dressed[label] = (*armour(prefix, "SHP", f"{prefix}_ARM_FBD{letter}_NH_MDL"), *identity)
            if letter == "g" and not female:
                dressed[label] = (*armour(prefix, "SHP", f"{prefix}_ARM_NH_FBDg_MDL"), *identity)
        for letter, dlc, suffix in (("i", "DLC_CON_APP01", ""), ("j", "DLC_EXP_Pack003", "_DLC")):
            label = f"Full-body suit {letter.upper()} (DLC)"
            package = f"BIOG_{prefix}_SHP_FBD_R{suffix}"
            suits[label] = parts(package, f"{prefix}_SHP_FBD{letter}_MDL", dlc=dlc)
            dressed[label] = (*parts(package, f"{prefix}_SHP_FBD{letter}_NH_MDL", dlc=dlc), *identity)
        suits["Full-body suit (SHPi)"] = armour(prefix, "SHP", f"{prefix}_ARM_SHPi_MDL")
    casual_variants = {"LE1": "abcdefgh", "LE2": "abcdefghjk" if female else "abcdefghikl",
                       "LE3": "abcdefghijk" if female else "abcdefghijkl"}
    civilian = {"Casual outfit" if letter == "a" else f"Casual outfit {letter.upper()}":
                (*armour(prefix, "CTH", f"{prefix}_ARM_CTH{letter}_MDL"), *identity)
                for letter in casual_variants[game]}
    if game == "LE2":
        civilian["Formal outfit (DLC)"] = (*parts(f"BIOG_{prefix}_SHP_CTH_R",
                                                   f"{prefix}_ARM_CTHa_Tux_MDL", dlc="DLC_HEN_MT"), *identity)
        body = parts("BioD_EndGm1_110ROMGarrus", "HMF_ARM_NKDa_MDL") if female else ()
    else:
        body = armour(prefix, "NKD", f"{prefix}_ARM_NKDa_MDL")
    if game == "LE3":
        formal = "HMF_ARM_CTHl_MDL" if female else "BIOG_HMM_FRM_MDL"
        civilian["Citadel formalwear"] = (*parts(f"BIOG_{prefix}_FRM_SHP_R", formal,
                                                 dlc="DLC_EXP_Pack003"), *identity)
    return Character(identity, suits, body, civilian, dressed)


# LE1 squad members wear the shared armour meshes for their body type (picked by the
# equipped armour); their henchman packages only add heads and the BRT breather helmets.
LE1 = {
    "Ashley": Character(parts("BIOG_HumanFemale_Hench_C", "HMF_HED_PRO_Ashley_MDL_LOD0",
                              "HMF_HIR_PROAshley_MDL"),
                        {**le1_armour("HMF"),
                         "Breather helmet": parts("BIOG_HumanFemale_Hench_C", "HMF_BRT_LGT_MDL")},
                        body=parts("BIOA_NOR10_09ashley_DS2", "HMF_ARM_NKDa_MDL"),
                        body_look=parts("BIOA_NOR10_09ashley_DS2", "HMF_ARM_NKDa_MDL",
                                        "HMF_HED_PRO_Ashley_MDL_LOD0", "HMF_HIR_PROAshley_MDL")),
    "Garrus": Character(parts("BIOG_Turian_Hench_C", "TUR_HED_PROBase_MDL", "TUR_HGR_VSRa_MDL"),
                        {"Light armour": armour("TUR", "LGT", "TUR_ARM_LGTa_MDL"),
                         "Light armour B": armour("TUR", "LGT", "TUR_ARM_LGTb_MDL"),
                         "Medium armour": armour("TUR", "MED", "TUR_ARM_MEDa_MDL"),
                         "Heavy armour": armour("TUR", "HVY", "TUR_ARM_HVYa_MDL"),
                         "Breather helmet": parts("BIOG_Turian_Hench_C", "TUR_BRT_HVYa_MDL")}),
    "Kaidan": Character(parts("BIOG_HumanMale_Hench_C", "HMM_HED_PROKaiden_MDL", "HMM_HIR_Kai_MDL"),
                        {**le1_armour("HMM"),
                         "Breather helmet": parts("BIOG_HumanMale_Hench_C", "HMM_BRTb_MED_MDL")}),
    "Liara": Character(parts("BIOG_Asari_Hench_C", "ASA_HED_PROBASE_MDL"),
                       {**le1_armour("HMF"),
                        "Breather helmet": parts("BIOG_Asari_Hench_C", "HMF_BRT_MEDb_MDL")}),
    "Tali": Character((), {"Default suit": armour("QRN", "LGT", "QRN_FAC_ARM_LGTa_MDL")}),
    "Wrex": Character(parts("BIOG_Krogan_Hench_C", "KRO_HED_PROBase_MDL"),
                      {"Heavy armour": armour("KRO", "HVY", "KRO_ARM_HVYa_MDL"),
                       "Heavy armour B": armour("KRO", "HVY", "KRO_ARM_HVYb_MDL"),
                       "Heavy armour C": armour("KRO", "HVY", "KRO_ARM_HVYc_MDL"),
                       "Medium armour": armour("KRO", "MED", "KRO_ARM_MEDa_MDL")}),
}

LE2 = {
    "Garrus": squad("Garrus", "TUR_ARM_HVYb_MDL", "TUR_HED_PROGarrus_Damage_MDL", alt="TUR_ARM_HVYb_MDL",
                    extras=(("DLC suit", "02", "TUR_ARM_HVYc_MDL", "DLC_CON_Pack01", "TUR_HED_Garrus_ALT_MDL"),)),
    "Grunt": squad("Grunt", "KRO_GNT_LGTa_MDL", "KRO_HED_Grunt_MDL", alt="KRO_GNT_LGTa_MDL",
                   extras=(("DLC suit", "02", "KRO_GNT_LGTa_MDL", "DLC_CON_Pack02", "KRO_HED_Grunt_ALT_MDL"),)),
    "Jack": squad("Convict", "HMF_JCK_LGTa_MDL", "HMF_HED_PROJack_MDL", alt="HMF_JCK_LGTa_MDL",
                  extras=(("DLC suit", "02", "BIOG_HMF_JCK_ALT_R.LGTa.HMF_JCK_LGTa_MDL", "DLC_CON_Pack01",
                           "HMF_HED_PROJack_ALT_MDL"),)),
    "Jacob": squad("Leading", "HMM_ARM_MEDe_MDL", "HMM_HED_PROJacob_MDL", alt="HMM_ARM_MEDe_MDL"),
    "Kasumi": squad("Thief", "HMF_KSM_LGTa_MDL", "HMF_HED_PROKasumi_MDL",
                    alt="HMF_KSM_LGTa_MDL", dlc="DLC_HEN_MT"),
    "Legion": squad("Geth", "GTH_LEG_NKDa_MDL", "GTH_HED_PROLegion_MDL", alt="GTH_LEG_NKDa_MDL"),
    "Liara": squad("Liara", "ASA_ARM_LGTa_MDL", "ASA_HED_PROBASE_MDL", dlc="DLC_EXP_Part01"),
    "Miranda": squad("Vixen", "HMF_MIR_LGTa_MDL", "HMF_HED_PRO_Miranda_MDL", "HMF_HIR_MIR_MDL",
                     alt="HMF_MIR_LGTa_MDL",
                     extras=(("DLC suit", "02", "HMF_MIR_LGTc_MDL", "DLC_CON_Pack02"),)),
    "Morinth": Character(parts("BioD_Nor_231Morinth", "ASA_HED_PROMorinth"),
                         {"Squad suit": parts("BioH_Mystic_00", "ASA_ARM_LGTa_MDL")},
                         civilian={"Alternate outfit": parts("BioD_Nor_231Morinth", "HMF_ARM_LGTc_MDL",
                                                            "ASA_HED_PROMorinth")}),
    "Mordin": squad("Professor", "SAL_ARM_LGTb_MDL", "SAL_HED_PROMordin_MDL", alt="SAL_ARM_LGTb_MDL"),
    "Samara": squad("Mystic", "ASA_ARM_LGTa_MDL", "ASA_HED_PROSamara_MDL", alt="ASA_ARM_LGTa_MDL"),
    "Tali": squad("Tali", "QRN_TLI_LGTa_MDL", alt="QRN_TLI_LGTa_MDL",
                  extras=(("DLC suit", "02", "QRN_TLI_LGT_ALT_MDL", "DLC_CON_Pack02"),)),
    "Thane": squad("Assassin", "DRL_THN_LGTa_MDL", "DRL_HED_PROTHANE_MDL", alt="DRL_THN_LGTa_MDL",
                   extras=(("DLC suit", "02", "DRL_THN_LGTa_ALT_MDL", "DLC_CON_Pack01", "DRL_HED_THANE_ALT_MDL"),)),
    "Zaeed": squad("Veteran", "HMM_Zaeed_LGTa_MDL", "HMM_HED_PROZaeed_MDL",
                   alt="HMM_Zaeed_LGTa_MDL", dlc="DLC_HEN_VT"),
}

LE3 = {
    "Aria": squad("Aria", "ASA_ARM_ARA_MDL", "ARIA_HED_MDL", dlc="DLC_EXP_Pack002"),
    "Ashley": squad("Ashley", "HMF_ARM_ASHa_MDL", "HMF_HED_PROAshley_MDL", "HMF_HIR_PROAshley_MDL",
                    alt="HMF_ARM_ASHb_MDL",
                    extras=(("Collector's Edition suit", "02", "HMF_ARM_ASHa_MDL", "DLC_OnlinePassHidCE"),
                            ("DLC suit", "03", "HMF_ARM_ASHf_MDL", "DLC_HEN_PR")),
                    civilian={"Citadel casual": parts("BioH_Exp3_Ashley_1", "HMF_ARM_CTHl_MDL",
                                                      "HMF_HED_PROAshley_MDL", "HMF_HIR_PROAshley_MDL", dlc=CITADEL)}),
    "EDI": squad("EDI", "HMF_ARM_EDIa_MDL", "HMF_HED_PROEdi_MDL", alt="HMF_ARM_EDIb_MDL",
                 extras=(("From Ashes suit", "02", "HMF_ARM_EDIb_MDL", "DLC_HEN_PR"),
                         ("Appearance Pack suit", "03", "HMF_ARM_EDIf_MDL", "DLC_CON_APP01"))),
    "Garrus": squad("Garrus", "TUR_ARM_GARa_MDL", "TUR_HED_PROGarrus_MDL", alt="TUR_ARM_GARb_MDL",
                    extras=(("From Ashes suit", "02", "TUR_ARM_GARc_MDL", "DLC_HEN_PR"),
                            ("Appearance Pack suit", "03", "TUR_ARM_GARe_MDL", "DLC_CON_APP01")),
                    civilian={"Citadel casual": parts("BioH_Exp3_Garrus_1", "TUR_ARM_CTHc_MDL",
                                                      "TUR_HED_PROGarrus_MDL", dlc=CITADEL)}),
    "James Vega": squad("Marine", "HMM_ARM_SANa_MDL", "BIOG_HMM_HED_James", "HMM_HIR_PROJames_MDL",
                        alt="HMM_ARM_SANc_MDL",
                        extras=(("Collector's Edition suit", "02", "HMM_ARM_SANa_MDL", "DLC_OnlinePassHidCE"),
                                ("DLC suit", "03", "HMM_ARM_SANf_MDL", "DLC_HEN_PR")),
                        civilian={"Normandy outfit": (*parts("BioH_Marine_00_NC", "HMM_ARM_SANb_MDL",
                                                               "HMM_HIR_PROJames_MDL"),
                                                       *parts("BioH_Marine_00", "BIOG_HMM_HED_James"))}),
    "Javik": squad("Prothean", "PRN_ARM_LGTa_MDL", "PRN_HED_PRO_MDL",
                   extras=(("DLC suit", "01", "PRN_ARM_LGTa_MDL", "DLC_HEN_PR"),)),
    "Kaidan": squad("Kaidan", "HMM_ARM_KAIa_MDL", "HMM_HED_PROKaiden_MDL", alt="HMM_ARM_KAIb_MDL",
                    extras=(("Collector's Edition suit", "02", "HMM_ARM_KAIa_MDL", "DLC_OnlinePassHidCE"),
                            ("DLC suit", "03", "HMM_ARM_KAIe_MDL", "DLC_HEN_PR")),
                    civilian={"Normandy outfit": parts("BioH_Kaidan_00_NC", "HMM_ARM_CTHb_MDL",
                                                      "HMM_HED_PROKaiden_MDL")}),
    "Liara": squad("Liara", "ASA_ARM_LIAa_MDL", "ASA_HED_PROBASE_MDL", alt="ASA_ARM_LIAf_MDL",
                   extras=(("Collector's Edition suit", "02", "ASA_ARM_LIAe_MDL", "DLC_OnlinePassHidCE"),
                           ("From Ashes suit", "03", "HMF_ARM_ASHf_MDL", "DLC_HEN_PR"),
                           ("Appearance Pack suit", "04", "ASA_ARM_LIAh_MDL", "DLC_CON_APP01")),
                   civilian={"Citadel casual": parts("BioH_Exp3_Liara_1", "HMF_ARM_CTHc_MDL",
                                                     "ASA_HED_PROBASE_MDL", dlc=CITADEL)}),
    "Nyreen": squad("Nyreen", "TUF_ARM_NYR_MDL", "TUF_HED_NYR_MDL", dlc="DLC_EXP_Pack002"),
    "Tali": squad("Tali", "QRN_ARM_TLIa_MDL", alt="QRN_ARM_TLIb_MDL",
                  extras=(("DLC suit", "02", "QRN_ARM_TLIc_MDL", "DLC_HEN_PR"),)),
    "Wrex": squad("Wrex", "KRO_ARM_HVYa_MDL", "KRO_HED_PROWrex_MDL", dlc="DLC_EXP_Pack003",
                  civilian={"Citadel casual": parts("BioH_Exp3_Wrex_1", "KRO_ARM_CTHa_MDL",
                                                    "KRO_HED_PROWrex_MDL", dlc=CITADEL)}),
}


def crew(identity, looks, body=(), **options):
    return Character(identity, {}, body, looks, **options)


LE1.update({
    "Joker": crew(parts("BIOG_Pilot_Hench_C", "HMM_HED_PROJoker_MDL", "HMM_HAT_Cap_MDL"),
                  {"Pilot uniform": parts("BIOG_Pilot_Hench_C", "HMM_BTR_HVY_MDL",
                                          "HMM_HED_PROJoker_MDL", "HMM_HAT_Cap_MDL")}),
    "Dr. Chakwas": crew(parts("BIOA_NOR10_07_DSG", "ASA_HED_PROBASE_MDL", "HMF_HIR_Cte_MDL"),
                        {"Medical uniform": parts("BIOA_NOR10_07_DSG", "HMF_ARM_CTHh_MDL",
                                                  "ASA_HED_PROBASE_MDL", "HMF_HIR_Cte_MDL")}),
    "Richard Jenkins": crew(parts("BIOG_Jenkins_Hench_C", "HMM_HED_PROAverage_MDL"),
                           {"Alliance armour": parts("BIOG_Jenkins_Hench_C", "HMM_BTR_HVY_MDL",
                                                     "HMM_HED_PROAverage_MDL")}),
    "Charles Pressly": Character(
        parts("BIOA_NOR10_03_DSG", "HMM_HED_PROAverage_MDL"), {},
        face_morphs={parts("BIOA_NOR10_03_DSG", "HMM_HED_PROAverage_MDL")[0]: "nor10_navigator"},
    ),
    "David Anderson": crew(parts("BIOA_NOR10_03_DSG", "HMM_HED_PROAnderson_MDL"),
                           {"Alliance armour": parts("BIOA_NOR10_03_DSG", "HMM_ARM_MEDa_MDL",
                                                     "HMM_HED_PROAnderson_MDL")}),
})

LE2.update({
    "David Anderson": crew(parts("BioD_CitHub_CouncilTower", "HMM_HED_PROAnderson_MDL"),
                           {"Citadel uniform": parts("BioD_CitHub_CouncilTower", "HMM_ARM_CTHa_MDL",
                                                     "HMM_HED_PROAnderson_MDL")}),
    "Wrex": crew(parts("BioD_KroHub_100MainHub", "KRO_HED_PROBase_MDL"),
                 {"Tuchanka armour": parts("BioD_KroHub_100MainHub", "KRO_ARM_HVYa_MDL", "KRO_HED_PROBase_MDL")},
                 face_morphs={parts("BioD_KroHub_100MainHub", "KRO_HED_PROBase_MDL")[0]: "hench_krogan"}),
    "Ashley": crew(parts("BioD_HorCr1_303AshKaidan", "HMF_HED_PRO_Ashley_MDL_LOD0", "HMF_HIR_PROAshley_MDL"),
                   {"Horizon armour": parts("BioD_HorCr1_303AshKaidan", "HMF_ARM_HVYa_MDL",
                                            "HMF_HED_PRO_Ashley_MDL_LOD0", "HMF_HIR_PROAshley_MDL")}),
    "Kaidan": crew(parts("BioD_HorCr1_303AshKaidan", "HMM_HED_PROAverage_MDL", "HMM_HIR_Kai_MDL"),
                   {"Horizon armour": parts("BioD_HorCr1_303AshKaidan", "HMM_ARM_LGTa_MDL",
                                            "HMM_HED_PROAverage_MDL", "HMM_HIR_Kai_MDL")}),
    "Gabby Daniels": crew(parts("BioD_Nor_350Henchmen", "HMF_Blank_World_LOD0", "HMF_HIR_Cte_MDL"),
                          {"Cerberus uniform": parts("BioD_Nor_350Henchmen", "HMF_ARM_CTHb_MDL",
                                                     "HMF_Blank_World_LOD0", "HMF_HIR_Cte_MDL")}),
    "Ken Donnelly": crew(parts("BioD_Nor_350Henchmen", "HMM_HED_PROAverage_MDL", "HMM_HIR_PROShort02_MDL"),
                         {"Cerberus uniform": parts("BioD_Nor_350Henchmen", "HMM_ARM_CTHb_MDL",
                                                    "HMM_HED_PROAverage_MDL", "HMM_HIR_PROShort02_MDL")}),
    "EDI": Character((), {}, head_variants={
        "AI hologram": parts("BioD_HorCr1_304Joker", "NCA_EDI_NKDa_MDL"),
    }),
    "Kelly Chambers": crew(parts("BioD_Nor_105CerberusCrew", "HMF_Blank_World_LOD0", "HMF_HIR_Mom_MDL"),
                           {"Cerberus uniform": parts("BioD_Nor_105CerberusCrew", "HMF_ARM_CTHb_MDL",
                                                      "HMF_Blank_World_LOD0", "HMF_HIR_Mom_MDL"),
                            "Cabin dance outfit": parts("BioD_Nor_002CabinKelly", "HMF_ARM_NKDb_MDL",
                                                         "HMF_Blank_World_LOD0", "HMF_HIR_Mom_MDL")},
                           head_variants={"Cabin head + hair": parts("BioD_Nor_002CabinKelly",
                                                                     "HMF_Blank_World_LOD0", "HMF_HIR_Mom_MDL")}),
    "Rupert Gardner": crew(parts("BioD_Nor_250Henchmen", "HMM_HED_PROAverage_MDL"),
                           {"Cerberus uniform": parts("BioD_Nor_250Henchmen", "HMM_ARM_CTHb_MDL",
                                                      "HMM_HED_PROAverage_MDL")},
                           face_morphs={parts("BioD_Nor_250Henchmen", "HMM_HED_PROAverage_MDL")[0]: "nor_cook"}),
    "Joker": crew(parts("BioD_Nor_101Cockpit", "HMM_HED_PROJoker_MDL", "HMM_HAT_Cap_MDL"),
                  {"Pilot uniform": parts("BioD_Nor_101Cockpit", "HMM_ARM_CTHb_MDL",
                                          "HMM_HED_PROJoker_MDL", "HMM_HAT_Cap_MDL")}),
    "Dr. Chakwas": crew(parts("BioD_Nor_270Medical", "ASA_HED_PROBASE_MDL", "HMF_HIR_Cte_MDL"),
                        {"Medical uniform": parts("BioD_Nor_270Medical", "HMF_ARM_CTHh_MDL",
                                                  "ASA_HED_PROBASE_MDL", "HMF_HIR_Cte_MDL")}),
})

LE3.update({
    "Jessica Allers": Character((), {}, civilian={
        "Reporter outfit": parts("BioD_End001_436CRAllers", "HMF_ARM_JESa_MDL", "HMF_HIR_PROJessica_MDL"),
    }),
    "Kelly Chambers": crew(parts("BioD_CitHub_Underbelly", "HMF_HED_PROBase_MDL", "HMF_HIR_Mom_MDL"),
                           {"Citadel outfit": parts("BioD_CitHub_Underbelly", "HMF_ARM_CTHe_MDL",
                                                   "HMF_HED_PROBase_MDL", "HMF_HIR_Mom_MDL"),
                            "Citadel outfit (alternate hair)": parts("BioD_CitHub_Underbelly", "HMF_ARM_CTHe_MDL",
                                                                       "HMF_HED_PROBase_MDL", "HMF_HIR_Cte_MDL")},
                           head_variants={"Head + alternate hair": parts("BioD_CitHub_Underbelly",
                                                                         "HMF_HED_PROBase_MDL", "HMF_HIR_Cte_MDL")}),
    "Gabby Daniels": crew(parts("BioD_CitHub_WardsFlux", "HMF_HED_PROBase_MDL", "HMF_HIR_Cte_MDL"),
                          {"Normandy uniform": parts("BioD_CitHub_WardsFlux", "HMF_ARM_CTHb_MDL",
                                                     "HMF_HED_PROBase_MDL", "HMF_HIR_Cte_MDL")}),
    "Ken Donnelly": crew(parts("BioD_CitHub_WardsFlux", "HMM_HED_PROAverage_MDL", "HMM_HIR_PROShort02_MDL"),
                         {"Normandy uniform": parts("BioD_CitHub_WardsFlux", "HMM_ARM_CTHb_MDL",
                                                    "HMM_HED_PROAverage_MDL", "HMM_HIR_PROShort02_MDL")}),
    "Dr. Michel": crew(parts("BioD_Nor_001Doctors", "HMF_HED_PROBase_MDL", "HMF_HIR_Cte_MDL"),
                       {"Medical uniform": parts("BioD_Nor_001Doctors", "HMF_ARM_CTHh_MDL",
                                                 "HMF_HED_PROBase_MDL", "HMF_HIR_Cte_MDL")}),
    "David Anderson": crew(parts("BioD_End001_411PadAnderson", "HMM_HED_PROAnderson_MDL"),
                           {"Alliance uniform": parts("BioD_End001_411PadAnderson", "HMM_ARM_ANDa_MDL",
                                                      "HMM_HED_PROAnderson_MDL")}),
    "Admiral Hackett": crew(parts("BioD_Nor_204bAndersonHackett", "BIOG_HMM_HED_Hackett_MDL", "HMM_HAT_Hac_MDL"),
                            {"Alliance uniform": parts("BioD_Nor_204bAndersonHackett", "HMM_ARM_CTHj_MDL",
                                                       "BIOG_HMM_HED_Hackett_MDL", "HMM_HAT_Hac_MDL")}),
    "Legion": crew(parts("BioD_Gth002_90Legion", "GTH_HED_LGNa_MDL"),
                   {"Geth platform": parts("BioD_Gth002_90Legion", "GTH_ARM_LGNa_MDL", "GTH_HED_LGNa_MDL")}),
    "Mordin": crew(parts("BioD_Nor_004Mordin", "SAL_HED_PROMordin_MDL"),
                   {"Default outfit": parts("BioD_Nor_004Mordin", "SAL_ARM_MDNa_MDL", "SAL_HED_PROMordin_MDL")}),
    "Thane": crew(parts("BioD_CitHub_HospitalP2", "DRL_HED_PROTHANE_MDL"),
                  {"Default outfit": parts("BioD_CitHub_HospitalP2", "DRL_ARM_THNa_MDL", "DRL_HED_PROTHANE_MDL")}),
    "Traynor": crew(parts("BioD_End002_000Traynor", "HMF_HED_PROBase_MDL", "HMF_HIR_Cls_MDL"),
                    {"Normandy uniform": parts("BioD_End002_000Traynor", "HMF_ARM_CTHb_MDL",
                                               "HMF_HED_PROBase_MDL", "HMF_HIR_Cls_MDL"),
                     "Citadel casual": parts("BioH_Exp3_Traynor_1", "HMF_ARM_CTHk_MDL",
                                             "HMF_HED_PROBase_MDL", "HMF_HIR_Cls_MDL", dlc=CITADEL)},
                    body=parts("BioD_Nor_135RomTraynor", "HMF_ARM_NKDc_MDL")),
    "Cortez": crew(parts("BioD_End002_000Cortez", "HMM_HED_PROAverage_MDL"),
                   {"Normandy uniform": parts("BioD_End002_000Cortez", "HMM_ARM_CTHb_MDL",
                                              "HMM_HED_PROAverage_MDL"),
                    "Citadel outfit": parts("BioH_Exp3_Cortez_1", "BIOG_HMM_FRM_MDL",
                                            "HMM_HED_PROAverage_MDL", dlc=CITADEL)},
                   body=parts("BioD_Nor_136RomCortez", "HMM_ARM_NKDa_MDL")),
    "Joker": crew((*parts("BIOG_HMM_HED_PROMorph", "HMM_HED_PROJoker_MDL"),
                   *parts("BioH_Exp3_Joker_0", "HMM_HAT_Cap_MDL", dlc=CITADEL)),
                  {"Pilot uniform": (*parts("BioH_Exp3_Joker_0", "HMM_ARM_CTHb_MDL",
                                            "HMM_HAT_Cap_MDL", dlc=CITADEL),
                                     *parts("BIOG_HMM_HED_PROMorph", "HMM_HED_PROJoker_MDL"))}),
    "Dr. Chakwas": crew(parts("BioD_Nor_001Doctors", "HMF_HED_PROBase_MDL", "HMF_HIR_Cte_MDL"),
                        {"Medical uniform": parts("BioD_Nor_001Doctors", "HMF_ARM_CTHh_MDL",
                                                  "HMF_HED_PROBase_MDL", "HMF_HIR_Cte_MDL")}),
})

LE3["Ashley"] = replace(
    LE3["Ashley"],
    body=parts("BioD_Nor_131RomAshley", "HMF_ARM_ASHc_MDL"),
    body_look=parts("BioD_Nor_131RomAshley", "HMF_ARM_ASHc_MDL", "HMF_HIR_PROAshley_MDL"),
    body_materials=("hmm_arm_ashc_mat_1a",),
    civilian={**LE3["Ashley"].civilian,
              "Citadel formalwear": parts("BioH_Exp3_Ashley_2", "LC_HMF_Ashley_Body_MDL",
                                          "LC_HMF_Ashley_Head_MDL", "LC_HMF_Ashley_Hair_MDL", dlc=CITADEL)},
)
LE3["Kaidan"] = replace(
    LE3["Kaidan"],
    body=parts("BioD_Nor_132RomKaidan", "HMM_ARM_NKDb_MDL"),
    body_look=parts("BioD_Nor_132RomKaidan", "HMM_ARM_NKDb_MDL"),
    body_materials=("biog_hmm_arm_nkd_mat2a",),
)

for name, outfit, *head in (
    ("Grunt", "KRO_ARM_GNTa_MDL", "KRO_HED_PROGrunt_MDL"),
    ("Jack", "HMF_ARM_JCKa_MDL", "HMF_HED_PROJack_MDL", "HMF_HIR_PROJack_MDL"),
    ("Jacob", "HMM_ARM_JCBa_MDL", "HMM_HED_PROJacob_MDL"),
    ("Kasumi", "HMF_ARM_KSMa_MDL", "HMF_HED_PROKasumi_MDL"),
    ("Miranda", "HMF_ARM_MIRa_MDL", "HMF_HED_PROMiranda_MDL", "HMF_HIR_PROMiranda_MDL"),
    ("Samara", "ASA_ARM_SAMa_MDL", "ASA_HED_PROSamara_MDL"),
    ("Zaeed", "HMM_ARM_ZADa_MDL", "HMM_HED_PROZaeed_MDL"),
):
    package = f"BioH_Exp3_{name}_0"
    looks = {"Default outfit": parts(package, outfit, *head, dlc=CITADEL)}
    if name in ("Miranda", "Samara"):
        casual = "HMF_ARM_CTHg_MDL" if name == "Miranda" else "HMF_ARM_CTHd_MDL"
        looks["Citadel casual"] = parts(f"BioH_Exp3_{name}_1", casual, *head, dlc=CITADEL)
    LE3[name] = crew(parts(package, *head, dlc=CITADEL), looks)


def roster(root, game):
    supporting = {"LE1": LE1, "LE2": LE2, "LE3": LE3}[game]
    characters = {"Shepard": shepard(game), "FemShep": shepard(game, female=True),
                  **dict(sorted(supporting.items(), key=lambda item: item[0].casefold()))}

    def installed(meshes):
        return all((root / package).is_file() for package, _ in meshes)

    result = {}
    for name, character in characters.items():
        suits = {label: meshes for label, meshes in character.suits.items() if installed(meshes)}
        civilian = {label: meshes for label, meshes in character.civilian.items() if installed(meshes)}
        head_variants = {label: meshes for label, meshes in character.head_variants.items() if installed(meshes)}
        if (suits or civilian or head_variants or character.identity) and installed(character.identity):
            body = character.body if installed(character.body) else ()
            dressed = {label: meshes for label, meshes in character.dressed.items()
                       if label in suits and installed(meshes)}
            result[name] = replace(
                character, suits=suits, body=body, civilian=civilian, dressed=dressed,
                body_look=character.body_look if installed(character.body_look) else (),
                head_variants=head_variants,
            )
    return result
