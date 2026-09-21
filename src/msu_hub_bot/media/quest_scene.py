"""Original vector illustrations for the small, offline quest demonstration."""

from functools import lru_cache
from typing import Literal

import resvg_py

type DemoPicture = Literal["shore", "yard", "tower", "boat", "rescued", "shelter"]


@lru_cache(maxsize=6)
def render_demo_scene(scene: DemoPicture) -> bytes:
    """Render owned artwork, without model inference, external assets or fonts."""
    daylight = scene in {"rescued", "shelter"}
    sky = "#a6c6d3" if daylight else "#132539"
    horizon = "#6b96a6" if daylight else "#304b5a"
    lit = scene in {"tower", "rescued"}
    beam = (
        '<path d="M 576 116 L 900 48 L 900 212 Z" fill="#fff0ac" opacity=".3"/>'
        '<path d="M 530 116 L 80 54 L 80 204 Z" fill="#fff0ac" opacity=".14"/>'
        if lit
        else ""
    )
    detail = {
        "shore": '<path d="M95 350 L172 312 L179 350" stroke="#a4bbbf" stroke-width="4" fill="none"/>',
        "yard": '<rect x="335" y="293" width="94" height="59" rx="5" fill="#718676"/><circle cx="364" cy="321" r="17" fill="#203b41"/><path d="M390 304 L410 304 M390 315 L410 315" stroke="#ccd1b1" stroke-width="4"/>',
        "tower": '<rect x="496" y="340" width="128" height="22" fill="#f3e3ae" opacity=".5"/>',
        "boat": '<path d="M104 356 L241 356 L213 389 L132 389 Z" fill="#bd8868"/><path d="M183 302 L183 355 L216 351 Z" fill="#e0d5b5"/>',
        "rescued": '<path d="M110 337 L294 337 L260 371 L141 371 Z" fill="#c95848"/><rect x="173" y="312" width="64" height="24" fill="#e7e0c8"/><rect x="184" y="321" width="19" height="9" fill="#324e60"/>',
        "shelter": '<rect x="329" y="292" width="119" height="62" fill="#ba8f6f"/><path d="M314 292 L389 250 L463 292 Z" fill="#536858"/><rect x="369" y="306" width="34" height="48" fill="#ffd28b"/>',
    }[scene]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="480" viewBox="0 0 900 480">
    <rect width="900" height="480" fill="{sky}"/>
    <circle cx="180" cy="96" r="34" fill="{"#f6e6bd" if daylight else "#cbdcd2"}"/>
    <path d="M0 213 Q120 168 250 215 T520 210 T900 196 L900 480 L0 480 Z" fill="{horizon}"/>
    <path d="M0 309 Q180 266 320 319 T650 297 T900 321 L900 480 L0 480 Z" fill="#21495b"/>
    <path d="M412 346 L476 282 L610 261 L694 302 L805 350 L720 394 L447 391 Z" fill="#324541"/>
    <path d="M482 303 L530 135 L575 135 L624 303 Z" fill="#ded2b1"/>
    <path d="M506 220 L524 157 L581 157 L599 220 Z" fill="#ae6556"/>
    <path d="M491 277 L505 228 L601 228 L615 277 Z" fill="#ae6556"/>
    {beam}
    <rect x="518" y="94" width="67" height="45" fill="#384b4e"/>
    <rect x="526" y="102" width="51" height="27" fill="{"#ffe6a0" if lit else "#8ba69c"}"/>
    <path d="M504 95 L551 64 L598 95 Z" fill="#ae6556"/>
    <rect x="537" y="263" width="26" height="40" rx="12" fill="#243b3d"/>
    {detail}
    <path d="M40 412 Q100 398 164 412 M280 445 Q375 429 451 445 M690 429 Q785 415 865 429" stroke="#689298" stroke-width="3" fill="none"/>
    </svg>'''
    return resvg_py.svg_to_bytes(svg_string=svg, skip_system_fonts=True)
