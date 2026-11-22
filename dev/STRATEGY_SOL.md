# Mech Royale overnight plan | Sat 23:00–Sun 10:00 CST

**Keep the official scorer occupied.** At rank 36, BB-F0805900 (go-giddy3, 6.17) is your fallback. Beat the 32nd-place score strictly and keep drawing while the line can move. At 6.19, a new tie loses. With 48 integer placement scores, 297 points displays as 6.19 and 298 as 6.21.

## Submission queue

B = fresh RNG salt on go-619 (0e45c8d). G = fresh RNG salt on go-giddy3 (3ec3a38), the family that delivered 6.17. Use unsubmitted g619s/gg3s commits first, then fresh salts. Keep the respective wrappers and behavior fixed. Neither family has established a better upper tail, so alternate roughly 3 B to 2 G.
P = best eligible practice challenger below. F = at most one validated FFA build, based on the currently picked practice core and RNG salt. Replace any unavailable P or F slot with B/G immediately.

| CST start | Five scoring starts per full hour, in priority order |
|---|---|
| 23:00 | B, G, B, G, B. Set up the dodge test and retrieve current optimizer results. |
| 00:00 | B, G, B, G, B. Continue the practice screen. |
| 01:00 | B, G, P, B, G. First challenger draw if it has cleared the screen. |
| 02:00 | B, P, G, P, B. Use the same P as at 01:00, with fresh salts. |
| 03:00 | B, P, G, B, G. Fourth P draw if the line has earned it. |
| 04:00 | B, G, P, B, G. Give a promoted P another draw, or debut a screened runner-up. |
| 05:00 | B, P, G, P, B. Continue the stronger line. |
| 06:00 | B, P, G, B, G. Decide which practice line gets the remaining slots. |
| 07:00 | B, G, P, B, G. F may replace one B with the qualification cushion below. |
| 08:00 | B, G, P, B, G. Last window for the one F attempt. |
| 09:00 | Three draws by ~09:30 on the strongest practice line. Use B/G if evidence is weak. |

Plan for ~53 starts, including any already running at 23:00. Submit as soon as scoring clears, within the displayed six/hour cap and one-in-flight rule. Keep a checked backup ready. Start an extra draw only if recent turnaround leaves time to score and pick. Aim to have the pick confirmed by 09:45 CST (15:45 UTC), ahead of the 16:00 UTC lock. The 10:00 ET time in topteams.md concerns exhibitions.

**Pick rule.** On each receipt, explicitly pick the highest completed eligible score and reload to confirm bot ID, displayed score and rank. A new high is worth banking even below the line or before its mean is known. Once safely above the line, an equal-score F can replace it only if the platform shows the same effective qualification standing and F clears the final tests. Check the board each hour and after each new high, then watch continuously near lock.

## What gets CPU and official draws

- Practice first. Prioritize cfg8 and at most one later leader from sh_prac, with cfg0 rerun on every seed block. Cfg8 is 5.52 vs 4.88 on the first 48, a selected early result in a partial sweep. Confirm the squad overrides take effect after mode detection. Use an independent 96 paired matches and extend promising borderline cases to 192 only if the result can affect submissions. A candidate earns real draws with a fresh paired gain around +0.5 and a plausible mechanism, or a positive paired interval and a consistent mechanism across blocks. Sim squad behavior is inferred and its rankings have failed on small gains.
- Spend up to four real draws per screened line. After two, bail if both are below 5.50 or the mean is ≤5.45. Bank any pick improvement regardless. After four, end routine allocation below a 5.76 mean. At 5.76–5.84, keep mixed B/G draws and allow up to four more if the line has already delivered a score near the live cutoff. At ≥5.85, override the hourly mix and give it 4 of each next 5 slots, with one B/G control, then reassess after eight draws. One lucky score earns the pick, a repeatable lift earns the queue. Finish the first line before trialing a runner-up unless the first hits a stop rule.
- Close drops (5.62/4 draws), finish (5.54/2), no-cube-runs (5.49/4), HP banking (4.25), lightning-first (4.73), and undirected parameter sweeps. Use the fixed-physics sim and isolated conformance runs, leaving host headroom. Stop the broad practice sweep if it crowds out paired tests or misses the 06:00 allocation decision.

## New practice bet | dodge only when the current move would get hit

In brain619.go:1848–1897, bullet dodge predicts closest approach to a stationary body, checks only horizontal miss distance, then blends a shove into the already selected movement at :851–864. It can react to a bullet above the cylinder, blocked by a wall, or already missing a moving mech. It can also miss a bullet aimed ahead of the mech. A false shove can undo movement toward cover or away from crossfire. Protocol §§6–7 expose projectile velocity, self velocity, cylinder dimensions and hard geometry, and movement takes time to accelerate.

Make one practice-triggered change after pracOn at the existing gun/NPC dodge hook. Predict the next ~0.25–0.35 s from current velocity toward the intended move, with acceleration and charge slowdown. Check the swept vertical cylinder and hard cover. Keep intended movement on a predicted miss. On a credible hit, choose the nearer safe lateral adjustment with better clearance. Cap work to a few imminent threats and protect zone/escape priorities. Preserve laser and mortar handling. This could clear +0.30 if it converts about one extra kill in 3 of 10 matches, but the code inspection only establishes a prediction gap.

Timebox the first implementation to one hour. Inspect ~20 representative recorded or simulated encounters for false shoves or missed intercepts, then run 48 paired matches and one fresh 96-pair block if promising. Measure dodge overrides, gun/NPC damage, kill count and score, plus damage and kills before 60 s. Advance only if overrides or incoming damage improve and score moves in both blocks, with the practice screen above. Drop it if the gap is rare, the gain comes only from implausible projectile behavior, or timing degrades. If it misses the first P slot, fill the slot with B/G. Avoid spending the night on a second rewrite.

## Final | default to the Go profile that qualified

- The fixed clone sim gives base ~7.6–7.8. Sound went 8.06/64 then 7.44/57, with a matched set-2 control pending. Finish that control before ranking it. In sh_tune cfg18 leads cfg7 at 8.25 vs 7.69 over 48 each. Cfg0 has only the first 16 (5.06). Compare on identical seeds/slots and confirm the actual FFA latch. Use cfg18, sound and base first, with cfg7 only if a leader drops. Pause FFA CPU when it competes with qualification work.
- For a finalist, run ~128 fresh paired games split between clones and a mixed field with a clone majority on the fixed engine. Rotate spawn slots. Promote only if mean placement points rise by ≥0.4 overall with a positive paired interval, signs agree across fields, and win/top-four rates and deaths before 60 s support the change. Limit to one build. Preserve quiet travel, hard cover and selective fights. Showdown observations support these behaviors, with loadout and survival correlated.
- Before using F, require a cushion of ≥0.10 above 32nd and rank ≤28, with enough time to finish scoring. Gate FFA changes on confirmed non-practice evidence. The condition !pracOn alone is true for the first 20 s in practice. Replay-check practice actions against the picked build, smoke-test the transition and Docker conformance under low load, then submit at the picked salt. Use the returned score and rank as the qualification check. A practice challenger also needs a transition smoke test, since a real FFA can temporarily enter pracOn before the death detector latches.

## Odds

With independent normal draws at mean 5.56 and SD 0.27, a 6.20 target gives ~0.9% per draw and ~38% over 53 base draws. At 6.25, 6.30 and 6.35 the corresponding 53-draw chances are ~25%, ~15% and ~9%. At the current integer-point hurdle of 298/48, the model gives ~36%. These are fixed-hurdle scenarios. The sparse observed tail, salt dependence, scoring delays and a rising line make actual qualification odds uncertain, and the 6.17 pick has already fallen behind. A true +0.30 lift at unchanged SD would yield ~10% per draw at 6.20, or ~65% across 10 draws. Four real draws can prioritize it, with substantial uncertainty about its true mean.

A symmetric 16-bot field implies 1-in-16 to win the series once in it. If the top 32 first play for 16 final spots, reaching those spots is a separate hurdle. Clone-sim points and exhibition results do not identify our series win probability. Preserve every real qualification chance before spending a slot on a possible FFA edge.
