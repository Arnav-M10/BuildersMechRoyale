"""Regression coverage for grown-mech wall sliding and eave escape."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
import unittest

import geo
from dev.sim.engine import Actor, DT, Match


ROOT = Path(__file__).resolve().parents[2]


def load_config():
    with gzip.open(ROOT / "dev/check/stream.jsonl.gz", "rt", encoding="utf-8") as f:
        return json.loads(next(f))["config"]


class CylinderCollisionRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.geo = geo.Geo.load(str(ROOT / "arena"), str(ROOT / "cache.pkl"))

    def make_match_actor(self, pos, cubes=15):
        match = Match.__new__(Match)
        match.geo = self.geo
        match.cfg = self.config
        match.mechanics = self.config["mech"]
        match.actors = {}
        match.t = 0.0
        mech = dict(self.config["mech"])
        actor = Actor(0, "mech", [pos[0], pos[1], self.geo.ground(*pos)],
                      [0.0, 0.0, 0.0], [0.0, 0.0], 1400.0, 100.0,
                      cubes=cubes, stats=mech, max_hp=1400.0, max_energy=100.0)
        actor._move = [0.0, 0.0]
        actor._boost = False
        actor._jump = False
        match.actors[actor.id] = actor
        return match, actor

    def tick(self, match, actor, count):
        for _ in range(count):
            match.t += DT
            match._physics_actor(actor, DT)

    def test_grown_mech_slides_along_wall_at_279_37(self):
        match, actor = self.make_match_actor((279.0, 37.0), cubes=15)
        start = actor.pos[:]
        actor._move = [-1.0, -0.06]
        actor._boost = True
        actor._jump = True
        self.tick(match, actor, 1)
        actor._jump = False
        self.tick(match, actor, 99)

        moved = ((actor.pos[0] - start[0]) ** 2 + (actor.pos[1] - start[1]) ** 2) ** 0.5
        self.assertGreater(moved, 8.0, "the 15-cube mech should slide clear instead of wedging")
        self.assertLess(actor.pos[0], start[0] - 4.0, "the requested westward wall slide should make progress")
        self.assertEqual(match._cylinder_overlap(actor.pos[0], actor.pos[1], actor.pos[2],
                                                 *match._actor_size(actor)), 0)
        self.assertTrue(actor.alive)

    def test_grown_mech_slides_out_from_low_bridge_eave(self):
        # This bridge's underside is 11.26 m above terrain near (-103, -24):
        # low enough to overlap a 15-cube cylinder, even though a base mech fits.
        match, actor = self.make_match_actor((-104.0, -26.0), cubes=15)
        start = actor.pos[:]
        actor._move = [0.0, 0.0]
        actor._boost = True
        actor._jump = True
        self.tick(match, actor, 1)
        self.tick(match, actor, 9)

        under_eave_distance = ((actor.pos[0] - start[0]) ** 2 +
                               (actor.pos[1] - start[1]) ** 2) ** 0.5
        self.assertGreater(under_eave_distance, 2.0, "the eave should push the mech outward")
        self.assertAlmostEqual(actor.pos[2], start[2], delta=0.1,
                               msg="jetting must stop while the grown cylinder still overlaps the eave")
        self.assertGreater(match._cylinder_overlap(actor.pos[0], actor.pos[1], actor.pos[2],
                                                   *match._actor_size(actor)), 0)

        self.tick(match, actor, 50)

        moved = ((actor.pos[0] - start[0]) ** 2 + (actor.pos[1] - start[1]) ** 2) ** 0.5
        self.assertGreater(moved, 1.0, "the overhang should slide the grown mech outward")
        self.assertEqual(match._cylinder_overlap(actor.pos[0], actor.pos[1], actor.pos[2],
                                                 *match._actor_size(actor)), 0,
                         "after sliding out, the cylinder should clear the bridge and jet upward")
        self.assertGreater(actor.pos[2], 8.59, "the mech should be able to jet above the bridge once clear")
        self.assertTrue(actor.alive)


if __name__ == "__main__":
    unittest.main()
