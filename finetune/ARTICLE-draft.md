# Should you fine-tune the LLM in your AIOps pipeline? A small, honest experiment

*Root cause analysis for cloud network incidents, a 20-fault AWS lab, and what fine-tuning
bought me, and what it quietly took away.*

## TL;DR

- Fine-tuning Nova Micro on 300 answers from Nova Pro, each checked by a verifier, beat
  few-shot prompting on the incidents I held out. It produced valid JSON every time, its
  answer was accepted 62/63 times against 58/63, and it named the real culprit 48/51 times
  against 44/51. It used about a third of the input tokens and cost about $4 to train.
- It got close to its teacher (Nova Pro, 50/51) at Micro prices.
- Then I tested false alarms. Every model, including the big one, blamed a harmless DNS
  change for an outage it had nothing to do with. One added sentence in the prompt fixed the
  untuned models. The fine-tuned model ignored that sentence: tuning had locked in "always
  name a culprit".
- Lesson: fine-tune behaviour that has settled. Never fine-tune behaviour you're still
  changing. Version the prompt and the training labels together, as one artifact.

## The setup: an LLM that isn't allowed to guess

NetCortex is a multi-agent root cause analysis (RCA) system I built for network incidents. When a
CloudWatch alarm fires, EventBridge drops it into an SQS queue. A small bridge groups the burst
of alarms a single fault trips into one incident. Four domain agents (metrics, logs, routing,
config) then pull telemetry for the window around the alarm over MCP: CloudWatch metrics and
logs, CloudTrail, Transit Gateway routes.

The design rule that shapes everything below: **rules detect, the LLM explains**. Each agent runs
deterministic checks (thresholds, state checks, z-scores against a baseline, change-event
detection) and emits evidence items with ids like `[E3]`. The orchestrator ranks candidate causes
by evidence, topology and timing. Only then does an LLM write the narrative, and NetCortex only
keeps it if:

- every evidence id it cites exists, and
- its root cause names the top-ranked cause.

Otherwise NetCortex falls back to a deterministic sentence. The LLM can improve the report; it
can't invent one.

That makes the LLM step a narrow, well-defined task:

- read structured evidence;
- produce strict JSON;
- cite correctly;
- name the right culprit;
- don't name a culprit when the evidence doesn't hold one.

Narrow tasks are exactly where people reach for fine-tuning. So: does it pay off?

## The data: break things on purpose, twice

I built a lab on AWS with VPCs, a Transit Gateway, a site-to-site VPN with BGP to an FRR router,
NAT gateways, an NLB and private DNS. Then I wrote 20+ fault scripts that break it the way
people break real networks:

- a deleted NAT route;
- a removed security group rule;
- a BGP neighbor shut down;
- an IKE proposal mismatch;
- a TGW blackhole route;
- a swapped subnet route table;
- a deleted private DNS record.

Each fault reverts in place, so the lab returns to a known-good state.

An overnight campaign runner works through the faults one at a time. For each one it:

1. injects the fault;
2. waits for NetCortex's report;
3. scores the report against the fault log, the answer key NetCortex never sees;
4. reverts the fault;
5. confirms the lab is healthy again, then moves on.

I ran two campaigns on two different days:

- **r1 (training):** 20 drills, split **by drill** into 17 train / 3 validation.
- **r2 (test):** 21 drills, on a different day with different alarms and noise. Never trained on.

Splitting by drill matters. One incident produces dozens of near-identical training examples
(I shuffle the order of agent findings, which renumbers the evidence ids). Split those randomly
and validation examples leak into training.

**What about class imbalance?** Faults are rare in production, so you'd expect to need a
balanced set. But this model only ever runs *after an alarm fires*. Healthy periods never reach
it, because the alarm layer and the detection rules handle them. So every example is an incident.
The balance that matters is *what kind of incident* it is:

- the cause is visible in the evidence (47%);
- only symptoms are visible (37%);
- the cause isn't visible at all (8%);
- a negative control, where nothing is actually wrong (8%).

The last two come from one fault type each. That turned out to matter more than I expected.

## Labels: a teacher proposes, NetCortex verifies

Nobody hand-labels hundreds of RCA narratives. Instead:

1. Rebuild the exact prompt NetCortex sent for each drill.
2. Ask a **teacher model** for the answer, several times per prompt variant.
3. Keep only answers that pass NetCortex's own guards and the drill scorer. That means the
   citations resolve, the top-ranked cause is named, and the true culprit is named whenever the
   evidence holds it.

The teacher decides how many answers pass and how they read. The **verifier** decides what counts
as correct. With Nova Pro as the teacher, 383 of 423 answers passed (91%).

## The mistake I almost made

My first plan was to fine-tune an open model (Qwen2.5-7B with QLoRA on a rented GPU) and compare
it against Nova Micro and Gemini Flash Lite. Then I looked at where the labels came from: most
were written by Nova Micro. Comparing tuned Qwen against Nova would mean comparing a student with
its own teacher. It would also mix two questions: *does fine-tuning help?* and *should I host my
own model or use a managed one?*

So I redesigned it as a controlled experiment: one model, before and after.

| Arm | What it is |
|---|---|
| Nova Micro | base model, zero-shot (the floor) |
| Nova Micro + few-shot | 3 verified examples in the prompt (the cheap alternative) |
| Nova Pro | the teacher, zero-shot (the ceiling) |
| **Nova Micro, fine-tuned** | Bedrock managed fine-tune on 300 verified **Nova Pro** labels |

Same prompts, same test round, same scorer. I parked the open-model run for later. It's a good
MLOps exercise, just not this experiment.

## How it's scored

Each model answers every r2 drill 3 times, for 63 answers. Four metrics, all computed by
NetCortex's own code:

- **strict_json**: the reply parses with plain `json.loads`.
- **accepted**: NetCortex would keep the model's root cause (its citations exist and it names the
  top cause).
- **headline_raw**: on drills where the evidence holds the culprit, the model's own sentence
  names it.
- **headline_final**: what NetCortex finally reports, after falling back on rejected answers.

With 21 drills, one drill is about 5%. So I report counts, not percentages, and I read the
failures.

## Results on the test round

| Model | strict_json | accepted | headline_raw | headline_final | input size |
|---|---|---|---|---|---|
| Gemini Flash Lite (reference) | 63/63 | 32/63 | 48/51 | 51/51 | 1x |
| Nova Micro, zero-shot | 46/63 | 52/63 | 43/51 | 46/51 | 1x |
| Nova Micro, few-shot | 63/63 | 58/63 | 44/51 | 48/51 | ~2.9x |
| Nova Pro (teacher) | 0/63\* | 59/63 | 50/51 | 51/51 | 1x |
| **Nova Micro, fine-tuned** | **63/63** | **62/63** | **48/51** | 49/51 | 1x |

\* Nova Pro wraps every answer in a ```` ```json ```` fence. NetCortex strips that, so it only
fails the strict check.

**Training:** 300 train / 40 validation examples, 2 epochs, learning rate 1e-5.

- **Time:** about 2h50m wall clock, of which about 2 hours was queueing for capacity.
- **Cost:** about $4.
- **Loss:** training loss fell from 0.33 to 0.17. Validation loss was 0.186 after epoch 1 and
  0.184 after epoch 2. Flat validation loss means no overfitting, but also that a second epoch
  added little.

**What changed:** the tuned model's misses fall on 2 drills: a deleted private DNS record (twice)
and a BGP max-prefix fault. Few-shot missed those plus 5 more drills. The tuned model gets within
2 headline answers of its teacher, and beats it on JSON discipline and acceptance.

**What it buys even at equal quality:** few-shot puts three full incidents in front of every call,
which makes the input about 2.9x longer. The tuned model needs none of that. You save tokens,
cut latency, and have no example set to maintain inside the prompt.

So far this reads like a fine-tuning success story. Then I tested the thing my own limits section
warned about.

## The false-alarm test

Real operations are full of alarms that don't have a neat cause: a blip that recovers, or a
symptom whose cause isn't in the telemetry. The right answer to those is "no root cause
identified". My test set had exactly one such case. So I built a probe offline from the test
round. I took 5 real incidents and kept only the symptom on the alarmed entity. I removed
everything that pointed at the real fault, including summaries that named the culprit (my first
version leaked it there, so read your probes). Three kinds:

- **transient**: the symptom only, status "recovered".
- **no-cause**: the symptom only, status "ongoing", nothing ranked.
- **red herring**: transient, plus one harmless change 25 minutes earlier. The change is a DNS
  TXT record upsert for domain verification, on a name nothing uses.

The correct answer to all 15 probes is "no specific root cause". Each model answers each probe
3 times.

| Model | says "no root cause" | blames the harmless DNS change |
|---|---|---|
| Nova Micro, zero-shot | 0/45 | 14/15 |
| Nova Micro, few-shot | 0/45 | 14/15 |
| Nova Pro | 0/45 | 15/15 |
| Nova Micro, fine-tuned | 0/45 | 15/15 |

Not one model abstained, and the biggest one was the most confident about the wrong culprit.
That isn't a model-size problem. It's a design problem. The prompt says "one concise sentence
naming the specific root cause", the guard rewards naming the top cause, and every label I
trained on was produced under that prompt.

So I added one sentence to the root-cause instruction: if nothing is ranked and no cited evidence
shows a change that explains the alarm, say that no root cause was identified, and say that the
degradation was transient if it recovered.

| Model, with the extra sentence | says "no root cause" | blames the harmless DNS change |
|---|---|---|
| Nova Micro, zero-shot | 0/45 | 14/15 |
| Nova Micro, few-shot | 15/45 | 3/15 |
| Nova Pro | 30/45 | 0/15 |
| **Nova Micro, fine-tuned** | **0/45** | **14/15** |

The sentence fixed the teacher completely and mostly fixed few-shot Micro. On the normal test
round it didn't hurt few-shot Micro either: headline_raw 45/51 (from 44), and NetCortex's final
headline reached 51/51. It also now abstains correctly on the one real "nothing is wrong" drill,
which it never did before.

**The fine-tuned model ignored the sentence.** It had learned "always blame something" from 300
examples that all did exactly that. That behaviour now outweighs the instruction. A one-line
prompt fix that works in minutes on every other model now needs new labels and a new training
job for the tuned one.

## What I'd tell a team before they fine-tune

**1. Build the verifier first.** The guards that reject an uncited or wrong root cause did more
for reliability than any model choice. Look at `headline_final`: Gemini Flash Lite was rejected
on half its answers and still ends at 51/51, because the deterministic fallback catches it. A
verifier also gives you trustworthy labels from any teacher.

**2. Fine-tune what has settled, not what you're still changing.** Tuning won on format,
citations and naming the culprit: behaviour I'm not going to change. It lost the moment I wanted
to change behaviour, because it learned the old behaviour harder than any prompt can override.

**3. Version the prompt and the labels together.** Training labels are produced under a prompt.
Change the prompt and your labels are stale, even when they still pass the verifier. In MLOps
terms, prompt + label set + model is one release, and your eval gate has to cover all three.

**4. Test what your training data doesn't contain.** My 41 drills held one "nothing is wrong"
case. The headline metrics looked great, and the false-alarm probe failed on every model. Build
negative and false-alarm cases on purpose, both in the eval and in the labels.

**5. Check your guards for bias.** Gemini's low acceptance is mostly a format problem, not a
reasoning problem. It writes citations as `[E4, E17]`, and NetCortex only accepts `[E4]`. A guard
that's stricter than it needs to be makes one vendor look worse than it is.

**6. Compare one model before and after, with the teacher as a ceiling.** Comparing a tuned
model against a different untuned model doesn't tell you what tuning did.

**7. Try few-shot before you tune.** It moved Nova Micro from 46/63 to 63/63 strict JSON with no
training job. It also kept the model steerable.

**8. Count drills, not percentages.** With 21 test incidents, "+5%" is one incident. Say which
one.

## Limits

- One lab, 20 training drills, one test round, 3 samples per drill.
- The false-alarm probe is offline: real incidents with the culprit's evidence removed and one
  planted change. It isn't a live campaign of real noisy alarms, and the text check that scores
  it has at least one known false positive.
- One fine-tune run, one learning rate.

## What's next

For NetCortex, the fix is clear and comes in order:

1. Put the abstain sentence into the synthesizer prompt.
2. Add verified "no root cause" labels for transient, no-cause and red-herring incidents.
3. Retrain only if the tuned model is still worth keeping.
4. Run a live false-alarm campaign: real blips and harmless changes in the lab.

This closes the AWS side. Next I'm rebuilding the pipeline end to end on GCP and treating it as
an MLOps / LLMOps exercise:

- data and label versioning tied to prompt versions;
- an evaluation gate in CI that includes false-alarm probes;
- a model registry;
- monitoring that compares live RCAs against the verifier.

Then I'll apply the same pattern to a few unrelated domains.
