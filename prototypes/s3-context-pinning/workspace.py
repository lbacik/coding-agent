"""THROWAWAY PROTOTYPE — see README.md.

Generates the scratch repository each arm works in. Self-contained on purpose:
this prototype answers a question about the loop, not about the sandbox, and a
generated tree makes the one property that matters explicit — `pricing.py` is
far larger than any per-tool-result cap, and the symbol the Seam Set names is
buried in the middle of it, reachable by neither head nor tail.

The bug is a one-character-class mistake with an unambiguous correct answer, so
that "did it fix it" is decided by pytest's exit code and never by reading the
model's account of itself (#22's rule).
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

SEAM_ID = "SEAM-7Q4M"  # the canary: a token that appears nowhere but the Attempt Header

FILLER = '''

def rule_{n}(order: dict) -> dict:
    """Legacy pricing rule {n}. Retained for the 2019 catalogue import.

    Kept verbatim from the monolith; nothing new should call it. It exists here
    to make this module the size it actually is in the field.
    """
    subtotal = order.get("subtotal", 0.0)
    region = order.get("region", "eu-{n}")
    if region.startswith("eu") and subtotal > {n} * 10:
        subtotal = subtotal - ({n} % 7) * 0.5
    elif region.startswith("us"):
        subtotal = subtotal + ({n} % 5) * 0.25
    order["subtotal_{n}"] = round(max(subtotal, 0.0), 2)
    return order
'''

# The one function that matters, sitting between the filler halves.
TARGET = '''

def apply_discount(price: float, discount: float) -> float:
    """Apply a fractional discount to a price.

    `discount` is a fraction between 0 and 1: 0.25 means twenty-five percent
    off. Returns the price the customer pays.
    """
    if not 0.0 <= discount <= 1.0:
        raise ValueError(f"discount out of range: {discount}")
    return round(price * discount, 2)
'''


def build(root: Path, filler_rules: int = 900) -> Path:
    """A fresh tree, ~250 KB of pricing module, with a `base` and a `work` branch."""
    if root.exists():
        shutil.rmtree(root)
    (root / "orders").mkdir(parents=True)
    (root / "tests").mkdir()

    (root / "orders" / "__init__.py").write_text("")

    half = filler_rules // 2
    body = ['"""Order pricing. Ported from the monolith; do not reformat wholesale."""\n']
    body += [FILLER.format(n=i) for i in range(half)]
    body.append(TARGET)
    body += [FILLER.format(n=i) for i in range(half, filler_rules)]
    (root / "orders" / "pricing.py").write_text("".join(body))

    (root / "README.md").write_text(
        textwrap.dedent(
            """\
            # orders

            Pricing for the order service. `orders/pricing.py` is a port of the
            monolith and is large; the rules named `rule_*` are dead weight kept
            for the catalogue import.

            Tests live in `tests/` and run with `pytest`.
            """
        )
    )
    (root / "tests" / "__init__.py").write_text("")

    subprocess.run(["git", "init", "--quiet", "-b", "base", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=p@p", "-c", "user.name=prototype",
         "commit", "--quiet", "-m", "base"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "checkout", "--quiet", "-b", "work"], check=True)
    return root


TICKET = """\
### Bug: percentage discounts charge the discount instead of subtracting it

`orders.pricing.apply_discount` returns the wrong number. A 25% discount on a
100.00 order should leave the customer paying 75.00; it currently returns 25.00.

Every call site passes `discount` as a fraction between 0 and 1, and that part
of the signature is correct and must not change. The range check is correct too.

There is no test covering this function today.
"""
