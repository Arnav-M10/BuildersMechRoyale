"""Paired A/B of knob variants in the offline sim.
usage: python3 dev/tune.py <matches> <seed> '<json list of {"name":..,"p":{...}}>'
Every variant plays the same seeds; reports mean and the paired difference vs the first variant."""
import json, os, re, subprocess, sys, math
n, seed, variants = int(sys.argv[1]), int(sys.argv[2]), json.loads(sys.argv[3])
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
res = {}
for v in variants:
    env = dict(os.environ, MF_P=json.dumps(v.get("p", {})))
    out = subprocess.run([sys.executable, "dev/sim/run.py", "--matches", str(n), "--seed", str(seed), "--jobs", os.environ.get("TUNE_JOBS", "8"),
                          "--no-validate", "--", sys.executable, "-u", v.get("bot", "bot.py")],
                         cwd=here, env=env, capture_output=True, text=True).stdout
    sc = {int(m.group(1)): int(m.group(2)) for m in re.finditer(r"seed (\d+): place \d+ score (\d+)", out)}
    res[v["name"]] = sc
    base = res[variants[0]["name"]]
    common = sorted(set(sc) & set(base))
    d = [sc[k] - base[k] for k in common]
    mean = sum(sc.values()) / max(1, len(sc))
    md = sum(d) / max(1, len(d))
    sd = math.sqrt(sum((x - md) ** 2 for x in d) / max(1, len(d) - 1)) / math.sqrt(max(1, len(d)))
    print("%-22s n=%3d mean %.3f   diff vs base %+.3f ± %.3f" % (v["name"], len(sc), mean, md, sd), flush=True)
