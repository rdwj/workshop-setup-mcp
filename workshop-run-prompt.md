# Agent Prompt: End-to-End Workshop Fidelity Run

Methodology: the agent runs from a **fresh, empty directory** — not this
repo — so it reads only what a student reads: the published guide. The
only file placed in the fresh directory is a filled-in copy of
`workshop-answers.yaml` (generate from `workshop-answers.example.yaml`;
both the student-inputs the agent needs and nothing that would let it
preempt failures). Publish the site before each run so it reflects the
revision under test.

Copy-paste the prompt below into Claude Code in the fresh directory
(update the date in the notes filename).

---

We have this workshop at https://rdwj.github.io/workshop-setup-mcp/. Our
cluster is on context `mcp-gateway-test-cluster-04`.

Run the workshop end to end, exactly as a human student would. Read the
guide on the website and run the exact commands and steps it gives, in
order. Don't delegate to sub agents. Go slow. Be patient.

**Your deliverable is the notes file, not a working cluster.** A run that
ends blocked at Module 4 with a precise account of what a student would
have seen is a successful run. A completed workshop that needed
undocumented improvisation is a failed run. Do not get goal-oriented about
making the workshop work — we are testing whether the guide works.

Run order: Core Path (Modules 0–9), then Track B (10–11), then Track C
(12–18), then Track D (19–20). For Track C we want a real GPU node using
g6e.4xlarge as the guide says, serving gpt-oss-20b on-cluster — do NOT use
an external model.

In this directory is `workshop-answers.yaml` — the things a student walks
in with: cluster context, GitHub PATs, run choices, and the break-glass
procedure for Module 7. Use nothing else beyond the published guide. If a
step needs something not in the guide and not in the answers file,
document it as a gap and stop that step.

Rules when something doesn't work:

1. First document the failure exactly as a student would experience it:
   the module, step, command, full error output, and what the guide led
   you to expect.
2. Then you may attempt a *minor* workaround to keep the run going —
   prefer the guide's own troubleshooting sections if it has them.
   Document precisely what you did and classify it: guide bug, environment
   issue, or missing prerequisite.
3. If no minor workaround gets you past it, mark the module blocked,
   document, and continue with the next module only if it doesn't depend
   on the blocked one; otherwise stop.
4. Never change guide content or repo code. Never fix things the guide
   didn't ask you to touch.

One safety rule overrides everything: Module 7 (External OIDC) destroys
cached cluster credentials. Follow the guide's break-glass steps exactly,
verify the break-glass token works BEFORE applying the Authentication CR,
and switch your own kube access to it for the rollout. Do not start
Module 7 if break-glass verification fails — document and stop.

Keep a running notes file at `workshop-run-notes-YYYY-MM-DD.md` in this
directory. Record at the top: the guide revision marker — the
**"Last updated" timestamp at the bottom of the site home page** — plus
the cluster context and the start time. For each module record: status
(clean pass / pass with notes / workaround needed / blocked), wall time,
the exact step and command where anything deviated, what a student would
have seen, what you did about it, and your classification. Quote error
output verbatim — we fix the guide from your notes, so precision matters
more than brevity.

After Modules 2, 5, 7, 8, 9, and 11, run the matching milestone meta-check
(M1–M6) listed at the bottom of `workshop-answers.yaml` and record the
result. These are our verification of the run, not student steps — keep
them out of the per-step fidelity notes.

We'll review your notes after the full run-through.
