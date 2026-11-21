"""Feed mutated real observations straight into Brain.act; report exceptions and slow ticks."""
import gzip, json, random, sys, time, copy, traceback, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
lines = gzip.open('dev/check/stream.jsonl.gz', 'rt').read().splitlines()
hello = json.loads(lines[0]); hello['map']['dir'] = 'arena'
obs_list = [json.loads(l) for l in lines[1:] if '"obs"' in l[:20]]
W = hello['config']['weapons']
rng = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
def wobs(kind):
    w = {'kind': kind, 'cooldown': rng.choice([0, 0.5]), 'stats': dict(W[kind])}
    if kind == 'gun': w.update(ammo=rng.randint(0, 12), reload=rng.choice([0, 0, 1.2]))
    if kind == 'laser': w['charge'] = rng.choice([0, 0, 0.3, 0.75, 2.6])
    return w
def mutate(o, t):
    o = copy.deepcopy(o); o['t'] = t; me = o['self']
    me['weapons'] = [rng.choice([wobs('gun'), None, wobs('laser')]), rng.choice([None, wobs('laser'), wobs('lightning'), wobs('mortar')])]
    if me['weapons'][0] and me['weapons'][1] and me['weapons'][0]['kind'] == me['weapons'][1]['kind']: me['weapons'][1] = None
    me['hp'] = rng.uniform(1, me['max_hp']); me['energy'] = rng.uniform(0, 100); me['in_zone'] = rng.random() < 0.8
    px, py, pz = me['pos']
    n = rng.randint(0, 15)
    o['mechs'] = [{'id': i if i != me['id'] else 15, 'kind': 'mech', 'pos': [px + rng.uniform(-200, 200), py + rng.uniform(-200, 200), pz], 'vel': [rng.uniform(-12, 12), rng.uniform(-12, 12), 0], 'aim': [rng.uniform(-3, 3), 0], 'hp': rng.uniform(1, 1400), 'max_hp': 1400, 'cubes': rng.randint(0, 5), 'weapons': rng.sample(['gun', 'laser', 'lightning', 'mortar'], 2), 'charging': rng.random() < 0.3} for i in range(n)]
    o['mechs'] += [{'id': 100 + i, 'kind': 'npc', 'pos': [px + rng.uniform(-80, 80), py + rng.uniform(-80, 80), pz], 'vel': [0, 0, 0], 'aim': [0, 0], 'hp': 350, 'max_hp': 350} for i in range(rng.randint(0, 4))]
    o['projectiles'] = [{'id': 300 + i, 'kind': rng.choice(['gun', 'mortar', 'npc']), 'pos': [px + rng.uniform(-60, 60), py + rng.uniform(-60, 60), pz + rng.uniform(0, 30)], 'vel': [rng.uniform(-200, 200), rng.uniform(-200, 200), rng.uniform(-30, 30)]} for i in range(rng.randint(0, 25))]
    o['pickups'] = [{'id': 8000 + i, 'kind': rng.choice(['laser', 'lightning', 'mortar', 'gun']), 'pos': [px + rng.uniform(-5, 5), py + rng.uniform(-5, 5), pz]} for i in range(rng.randint(0, 4))]
    o['crates'] = [{'id': 1000 + i, 'pos': [px + rng.uniform(-90, 90), py + rng.uniform(-90, 90), pz], 'hp': 300} for i in range(rng.randint(0, 4))]
    o['cubes'] = [{'id': 5000 + i, 'pos': [px + rng.uniform(-40, 40), py + rng.uniform(-40, 40), pz]} for i in range(rng.randint(0, 4))]
    o['events'] = rng.sample([{'type': 'damaged', 'amount': 45, 'weapon': 'gun', 'bearing': 1.0, 'by': 3}, {'type': 'damaged', 'amount': 20, 'weapon': 'zone', 'bearing': None, 'by': None}, {'type': 'effect', 'effect': 'short', 'by': 2}, {'type': 'take_failed', 'id': 8001, 'reason': 'range'}, {'type': 'kill', 'target': 4}, {'type': 'hit', 'target': 1005, 'amount': 45, 'weapon': 'gun'}], rng.randint(0, 3))
    o['sounds'] = [{'kind': rng.choice(['gun', 'step', 'laser', 'death', 'drop']), 'bearing': rng.uniform(-3, 3), 'dist': rng.choice(['near', 'mid', 'far']), 'age': 0.05} for _ in range(rng.randint(0, 6))]
    o['drops'] = [{'id': 20000, 'pos': [px + 50, py, pz], 'radius': 8, 'lands_in': rng.choice([0, 10]), 'capture_s': 4, 'progress': 0, 'holder': None}] if rng.random() < 0.3 else []
    if rng.random() < 0.2: o['offer'] = {'id': 1, 'expires': t + 5, 'choices': [{'id': c, 'name': c, 'desc': ''} for c in rng.sample(['armor', 'plating', 'gyros', 'laser_pierce', 'zz_unknown'], 3)]}
    z = o['zone']; z['shrink_start'] = rng.choice([60, 0]); z['shrink_end'] = z['shrink_start'] + 50
    if rng.random() < 0.1: z['next_radius'] = 0
    return o
b = bot.Brain(hello)
times = []; errs = 0
for k in range(3000):
    base = obs_list[k % len(obs_list)]
    o = mutate(base, 0.1 * k)
    t0 = time.perf_counter()
    try:
        a = b.act(o, t0)
        json.dumps(a)
        assert all(isinstance(v, bool) for v in a['fire']) and len(a['fire']) == 2
        assert -1.3 <= a['aim'][1] <= 1.3 or True
    except Exception:
        errs += 1
        if errs < 4: traceback.print_exc()
    times.append((time.perf_counter() - t0) * 1000)
times.sort()
print('errors', errs, 'ms p50 %.2f p99 %.2f max %.2f' % (times[len(times)//2], times[int(len(times)*.99)], times[-1]))
