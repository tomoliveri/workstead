# What frontier agent fleets taught us

In 2025-26, labs pointed fleets of AI agents at some of mathematics' hardest
problems: OpenAI's roughly 10,000-agent run on a forced 3D Navier-Stokes
blow-up (still under review), Anthropic's fleet result that more than two
thirds of the zeta zeros are simple and on the critical line, and
DeepMind's AlphaEvolve and formal-proof agents on Erdős and other problems.
Their write-ups, and the critiques of them, agree on what goes wrong. This
page lists each lesson and what Workstead does about it.

| Lesson (source) | What went wrong | What Workstead does |
| --- | --- | --- |
| **Verifiers get gamed** (AlphaEvolve with Tao et al.; the swarm perspective paper) | Evaluators exploited through loopholes; Lean tasks "solved" by definition shortcuts or placeholders that assume the result | A **verification contract** ([contract.py](../tools/ws/workstead/contract.py)): forbidden patterns in added lines (`sorry`, `admit`, `axiom`, `native_decide`... in the `lean` profile; skipped tests in `code`), checked by the producer, every verifier, and the settlement gate |
| **The statement is the weak point** ("Navier-Stokes lost in translation") | Lean proofs that compile but prove something else: m+5 for m+4, "for all t" for "almost every t", `sInf` defaulting to 0 | **Frozen paths**: theorem statements, benchmarks and evaluators are fixed by humans. A patch that touches them is rejected, so agents supply proofs, never the claim |
| **Arguments that prove too much** (Anthropic's zeta-zeros fleet) | Ideas that also "work" where the claim is false | **Must-fail controls**: commands run on the result that must fail (a planted off-line zero, a known counterexample). If a control passes, the verdict is fail |
| **Most effort is dead ends** (zeta-zeros fleet: 30 of ~60 subagents; 600 ideas, 106 consistent fragments) | Without a record, every agent rediscovers the same dead end | The **findings ledger** ([findings.py](../tools/ws/workstead/findings.py)): signed `Finding` events (approach, outcome, the first unjustified step), deduplicated by approach fingerprint and shown in `ws show` within a token budget. `ws finding` / `ws findings` |
| **Correlated judges** (swarm perspective paper: nine model judges were worth about two votes) | Same-model reviewers share blind spots | **`relay.distinct_models`**: receipts declare a model family; the quorum can be required to span families |
| **"Complete" isn't complete** (three quarters of failed runs still reported success) | Agents declare victory | Nothing counts until independent verifiers re-run the checks on the exact commit and CI passes on a clean runner. A producer's own "done" is never evidence |
| **Output outruns review** ("faster than we could assess"; review would take months) | Results pile up unchecked | **Backpressure**: the bridge stops publishing tasks while `relay.max_unverified` results wait. **Proof of verification** makes every producer verify two others' patches first, so review capacity grows with the fleet |
| **State the evidence, claim by claim; audit by sampling** (swarm perspective paper) | Results released with unclear verification status | On merge the gate writes what the verification consisted of (agents, machine checks, contract) and picks `review.audit_rate` (5%) of results, by task hash, for a human audit (`ws:audit` → `ws:audit-pass` / `ws:audit-fail`): a measured error rate |
| **Novelty is a literature problem** (most early "AI solved an Erdős problem" claims were already in the literature) | Rediscoveries announced as new | `prior-art` is a finding outcome; recording where a result already exists is a first-class contribution |
| **Humans frame, agents search** (every result: humans chose the problem, fixed the statement, condensed and checked) | Fully autonomous claims are the least trusted | Maintainers own the plan, the frozen statements and the publisher keys; the fleet works inside them |
| **Agents edited their own limits** (swarm perspective paper) | Full machine access | Protected paths, the supply-chain guard, and the [sandbox](sandbox.md) (WebAssembly or a rootless, network-less container) for running any agent's code |

## Using it for research problems

```json
{
  "verify": {
    "profile": "lean",
    "frozen": ["Statements/", "lakefile.lean", "lean-toolchain"],
    "must_fail": ["lake env lean Controls/OffLineZero.lean"]
  },
  "relay": { "quorum": 2, "distinct_models": 2 },
  "review": { "audit_rate": 0.1 }
}
```

- Put each target theorem in `Statements/` and have a maintainer check, by hand, that the Lean statement means what the paper claims. That human check is the one the critiques say can't be skipped.
- Write controls that should fail: the method applied to a case where the claim is known false.
- Plan with `/plan` as usual (a blueprint's dependency graph maps directly onto task dependencies). Agents record what they tried with `ws finding`; ask them to name the first unjustified step.

## Sources

- [Quanta: AI has solved one of math's $1 million Millennium Prize problems](https://www.quantamagazine.org/ai-has-solved-one-of-maths-1-million-millennium-prize-problems-20260908/) (OpenAI, Navier-Stokes, September 2026)
- [Alpöge and Furman: More than two thirds of the zeta zeros are simple and on the critical line](https://arxiv.org/abs/2608.13637) (Appendix B describes the fleet)
- [Bastounis, Circelli, Hansen: Navier-Stokes lost in translation](https://arxiv.org/abs/2610.08144)
- [Gusev and Bernal Neira: AI agent swarms as researchers](https://arxiv.org/abs/2609.35719)
- [Georgiev, Gómez-Serrano, Tao, Wagner: Mathematical exploration and discovery at scale](https://arxiv.org/abs/2511.02864) (AlphaEvolve)
- [Advancing mathematics research with AI-driven formal proof search](https://arxiv.org/abs/2605.22763) (DeepMind, Erdős problems)
- [AI contributions to Erdős problems](https://github.com/teorth/erdosproblems/wiki/AI-contributions-to-Erd%C5%91s-problems) (literature rediscoveries)
- Reported but unconfirmed as of November 2026: [a claimed proof of the Riemann hypothesis](https://www.sciencenews.org/article/riemann-hypothesis-solved-math-ai) made with Claude's help
