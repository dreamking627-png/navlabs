"""Diagnose price sources: python -m app.check HAL BEL"""
import asyncio, sys
from .sources import Yahoo, NSE
async def main(syms):
    for src in (Yahoo(), NSE()):
        try:
            r = await src.fetch(syms)
            print(src.name, "OK", {k: (str(v["ltp"]), str(v["prev_close"])) for k, v in r.items()})
        except Exception as e:
            print(src.name, "FAILED:", e)
asyncio.run(main(sys.argv[1:] or ["HAL", "BEL"]))