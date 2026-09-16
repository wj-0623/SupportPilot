"""Generate reviewed vertical templates from the canonical general-commerce pack."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import NotRequired, TypedDict

ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "domain_packs"


class VerticalSpec(TypedDict):
    name: str
    product_keywords: list[str]
    excluded_categories: list[str]
    window_days: NotRequired[int]


VERTICALS: dict[str, VerticalSpec] = {
    "apparel": {
        "name": "服饰电商客服",
        "product_keywords": ["尺码", "版型", "面料", "颜色", "搭配", "size", "fit"],
        "excluded_categories": ["personalized", "worn-item"],
    },
    "electronics": {
        "name": "3C 数码电商客服",
        "product_keywords": ["兼容", "型号", "参数", "序列号", "激活", "compatibility"],
        "excluded_categories": ["activated-digital-license"],
    },
    "beauty": {
        "name": "美妆个护电商客服",
        "product_keywords": ["肤质", "成分", "色号", "过敏", "保质期", "ingredients"],
        "excluded_categories": ["opened-cosmetics", "hygiene-sealed"],
    },
    "food": {
        "name": "食品生鲜电商客服",
        "product_keywords": ["配料", "过敏原", "保质期", "冷链", "破损", "allergen"],
        "excluded_categories": ["perishable", "opened-food"],
        "window_days": 7,
    },
    "home": {
        "name": "家居家电电商客服",
        "product_keywords": ["尺寸", "安装", "上门", "材质", "空间", "installation"],
        "excluded_categories": ["custom-made", "installed-item"],
    },
    "cross-border": {
        "name": "跨境电商客服",
        "product_keywords": ["关税", "清关", "币种", "海外仓", "禁运", "customs", "duties"],
        "excluded_categories": ["customs-rejected", "restricted-goods"],
        "window_days": 14,
    },
}


def main() -> None:
    base = json.loads((PACKS / "general-commerce.json").read_text(encoding="utf-8"))
    for slug, details in VERTICALS.items():
        pack = copy.deepcopy(base)
        pack["slug"] = slug
        pack["name"] = details["name"]
        pack["vertical"] = slug
        product = next(item for item in pack["intents"] if item["name"] == "product")
        product["keywords"] = list(dict.fromkeys(product["keywords"] + details["product_keywords"]))
        returns = pack["policies"]["returns"]
        returns["excluded_categories"] = details["excluded_categories"]
        returns["window_days"] = details.get("window_days", returns["window_days"])
        target = PACKS / f"{slug}.json"
        target.write_text(json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
