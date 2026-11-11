"""Category retrieval. The model (or rule) may only choose among retrieved candidates, so an
invented category is impossible by construction (ADR-07, AIR-11).

The built-in tree is a compact general-merchandise taxonomy. A destination's own taxonomy is
loaded the same way: (id, path, keywords).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

_RAW = """
apparel.men.shirts|Apparel > Men > Shirts|shirt tee t-shirt polo oxford button men mens
apparel.men.pants|Apparel > Men > Pants|pants trousers jeans chinos denim men mens
apparel.men.outerwear|Apparel > Men > Outerwear|jacket coat hoodie parka blazer men mens
apparel.women.dresses|Apparel > Women > Dresses|dress gown maxi midi sundress women womens ladies
apparel.women.tops|Apparel > Women > Tops|blouse top tunic camisole shirt women womens ladies
apparel.women.pants|Apparel > Women > Pants|leggings jeans trousers pants women womens ladies
apparel.kids|Apparel > Kids|kids boys girls toddler baby onesie romper
apparel.shoes|Apparel > Shoes|shoes sneakers boots sandals loafers heels trainers slippers
apparel.accessories|Apparel > Accessories|belt scarf hat cap gloves sunglasses wallet tie socks
bags.backpacks|Bags > Backpacks|backpack rucksack daypack
bags.handbags|Bags > Handbags|handbag purse tote clutch crossbody
jewelry.watches|Jewelry > Watches|watch chronograph smartwatch wristwatch
jewelry.necklaces|Jewelry > Necklaces & Rings|necklace ring bracelet earrings pendant jewelry
electronics.phones|Electronics > Phones & Accessories|phone smartphone case charger screen protector iphone android
electronics.computers|Electronics > Computers|laptop notebook desktop monitor keyboard mouse ssd
electronics.audio|Electronics > Audio|headphones earbuds speaker bluetooth soundbar microphone
electronics.cameras|Electronics > Cameras|camera lens tripod dslr mirrorless gopro
electronics.tv|Electronics > TV & Video|tv television projector streaming hdmi
electronics.cables|Electronics > Cables & Adapters|cable adapter usb hub hdmi charger power bank
home.kitchen|Home > Kitchen & Dining|pan pot knife cookware mug plate cutlery kettle blender kitchen
home.bedding|Home > Bedding|sheet duvet pillow blanket comforter mattress bedding
home.furniture|Home > Furniture|chair table sofa desk shelf cabinet stool furniture
home.decor|Home > Decor|vase frame candle rug curtain lamp wall art decor
home.storage|Home > Storage & Organization|storage box organizer basket hanger rack
home.cleaning|Home > Cleaning|vacuum mop detergent cleaner sponge broom
garden.tools|Garden > Tools|shovel rake hose pruner trowel garden lawn mower
garden.plants|Garden > Plants & Seeds|seed plant pot planter soil fertilizer
beauty.skincare|Beauty > Skincare|moisturizer serum cleanser sunscreen cream lotion skincare
beauty.makeup|Beauty > Makeup|lipstick mascara foundation eyeshadow concealer makeup
beauty.hair|Beauty > Hair Care|shampoo conditioner hair dryer straightener brush
beauty.fragrance|Beauty > Fragrance|perfume cologne fragrance eau de toilette
health.supplements|Health > Vitamins & Supplements|vitamin supplement protein capsule omega probiotic
health.personal|Health > Personal Care|toothbrush razor deodorant soap sanitizer
sports.fitness|Sports > Fitness|dumbbell yoga mat resistance band kettlebell treadmill gym
sports.outdoor|Sports > Outdoor & Camping|tent sleeping bag hiking camping backpacking stove
sports.cycling|Sports > Cycling|bike bicycle helmet cycling pedal saddle
sports.team|Sports > Team Sports|football soccer basketball cricket bat ball jersey
toys.games|Toys > Games & Puzzles|puzzle board game cards lego blocks toy
toys.dolls|Toys > Dolls & Plush|doll plush teddy stuffed action figure
baby.gear|Baby > Gear|stroller car seat crib diaper bottle pacifier baby
pets.dogs|Pets > Dogs|dog leash collar kibble puppy chew
pets.cats|Pets > Cats|cat litter scratching kitten
books.books|Media > Books|book novel paperback hardcover textbook
office.stationery|Office > Stationery|pen notebook paper stapler marker planner pencil
office.printers|Office > Printers & Ink|printer ink toner cartridge scanner
auto.parts|Automotive > Parts & Accessories|car tire brake wiper oil filter seat cover automotive
tools.power|Tools > Power Tools|drill saw grinder sander impact driver cordless
tools.hand|Tools > Hand Tools|hammer screwdriver wrench pliers tape measure toolkit
food.snacks|Food > Snacks|chips chocolate candy cookies nuts snack bar
food.beverages|Food > Beverages|coffee tea juice water soda drink
food.pantry|Food > Pantry|rice flour oil spice sauce pasta honey
"""

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "for", "with", "of", "in", "to", "new", "pack", "set", "pcs", "size", "color"}


@dataclass(frozen=True)
class Node:
    id: str
    path: str
    tokens: frozenset[str]


def tokenize(text: str) -> list[str]:
    out = []
    for token in _TOKEN.findall(text.lower()):
        if token in _STOP or len(token) < 2:
            continue
        out.append(token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token)
    return out


@lru_cache
def nodes() -> tuple[Node, ...]:
    out = []
    for line in _RAW.strip().splitlines():
        node_id, path, keywords = line.split("|")
        out.append(Node(node_id, path, frozenset(tokenize(keywords + " " + path))))
    return tuple(out)


@lru_cache
def _idf() -> dict[str, float]:
    df: Counter[str] = Counter()
    for node in nodes():
        df.update(node.tokens)
    n = len(nodes())
    return {token: math.log((n + 1) / (count + 0.5)) for token, count in df.items()}


def by_id(node_id: str) -> Node | None:
    return next((n for n in nodes() if n.id == node_id), None)


def retrieve(text: str, k: int = 5) -> list[tuple[Node, float]]:
    """Top-k candidates by IDF-weighted token overlap. Scores are normalized to [0, 1]."""
    query = Counter(tokenize(text))
    if not query:
        return []
    idf = _idf()
    scored = []
    for node in nodes():
        score = sum(idf.get(t, 0.0) * min(c, 2) for t, c in query.items() if t in node.tokens)
        if score > 0:
            scored.append((node, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))
    top = scored[:k]
    best = top[0][1] if top else 1.0
    return [(node, score / best * min(1.0, best / 6.0)) for node, score in top]
