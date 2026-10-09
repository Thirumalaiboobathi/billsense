# Billsense: an AWS bill explainer that tells you how worried to be

## 1. What it is and who it's for

Billsense is a small agent that explains an AWS bill in plain English. You ask "why did my bill go up?" and it answers the way a calm friend would. It is for people who open Cost Explorer, see a wall of services and regions, and only want to know whether to worry: students, solo builders, small teams with no FinOps person.

It only reads data. It never changes anything in your account.

## 2. How I built it

- **Strands Agents SDK (Python)** runs the agent loop and tool calls.
- **Amazon Bedrock, Amazon Nova Pro** (APAC inference profile) writes the answers.
- **Cost Explorer** supplies spend by service and region. Each call costs $0.01, so results are cached for 10 minutes.
- **EC2 Describe APIs** let a second tool find idle resources: NAT Gateways, unattached EBS volumes, unassociated Elastic IPs.
- **AWS Lambda with a Function URL**, deployed with SAM as a zip. One function serves the page and the API.

```
Browser (single index.html)
   |  GET /   POST /ask   POST /feedback
   v
Lambda Function URL --> Lambda (handler.py)
                          |-- agent.py: Strands + Nova Pro on Bedrock
                          |-- tools.py
                                |-- Cost Explorer (us-east-1)
                                |-- EC2 Describe* (ap-south-1, us-east-1)
```

The most important design choice: **the numbers come from the tools, not the model.** The charts, KPI cards and idle-resource table are built in Python from the tool output and sent to the page as structured data. The model writes the sentences; it never supplies the figures on screen. That matters for a bill, where a confident wrong number is worse than no answer.

There is also a demo mode with hardcoded sample data, so anyone can try it without AWS access.

## 3. The delightful details

**The Panic Meter (the main one).** Every answer starts with one verdict: 🟢 Relax., 🟡 Worth a look. or 🔴 Act now. Then at most three short bullets, then exactly one next step. People open a bill page with one question, "should I be worried?", so the answer comes first and the detail second. The format is in the system prompt and checked in code: if the reply doesn't start with one of the three emoji, the agent is asked once to rewrite it.

**A friendly first run.** The first visit shows a short welcome, a three-line legend of the Panic Meter, and one button: "Show me with demo data". It asks the first question for you.

**A glossary in plain English.** Service names like NAT Gateway, EBS and CloudWatch have a dotted underline. Tap one and you get one sentence on what it is and how it bills.

**Error cards that help.** Each error says what happened, why, and what to do, with one button: Retry, Re-enter passcode, Try demo data, a countdown when rate-limited, or a link on rotating a leaked key.

**A calm tone.** Short, kind wording. A small line above the verdict says "Nothing to worry about." or "Take a breath. Here's what to do first." No jokes about money.

## 4. Security, because it's my bill

- Real-account mode needs an **owner passcode**. Billsense never asks for AWS access keys.
- A **credential-leak guard** on both the page and the server blocks anything that looks like an access key before it reaches the model or the logs.
- Account IDs, ARNs and resource IDs are **masked** to the last four characters, in the answer and in the data.
- **Rate limits** on wrong passcodes and on all questions per IP, plus a $10/month budget alert.
- A strict **Content-Security-Policy** and other security headers.
- The logs hold only the request ID, mode, verdict, latency and tools used. **Questions and answers are not logged.**

Two limits to be honest about: the rate limits are in memory per warm Lambda container, so they are best-effort, and my account's concurrency quota was too low to set a reserved concurrency cap.

## 5. How I know it worked

I tested by hand, not with a test suite: the three starter questions in demo mode and on my own (almost idle) account, plus follow-ups, a pasted fake key, and 21 rapid requests to trigger the limit. Along the way I caught real problems: the model copying a date from my example, adding a delete command to an answer that didn't ask for one, and chart bars not re-coloring in dark mode. I fixed each one.

Under every answer there is a "Was this clear? 👍 👎" button. It records only the verdict and the choice.

- 👍 count: _[fill in]_
- 👎 count: _[fill in]_
- Tester quote 1: _[fill in]_
- Tester quote 2: _[fill in]_
- Tester quote 3: _[fill in]_

## 6. Proof

- Screenshots: first run, report (light and dark), glossary popover, security card. _[add from the screenshots folder]_
- Live demo: _[add Function URL]_ (choose "Demo data")
- Repository: _[add repo link]_

## 7. What's next

Connecting other accounts through a cross-account read-only role with a per-customer External ID, so people can try it on their own bill without sharing anything. I have not built this.

Tag: #agents
