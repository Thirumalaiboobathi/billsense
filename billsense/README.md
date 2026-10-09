# Billsense

Your AWS bill, explained calmly. Ask "why did my bill go up?" and get a plain-English answer that always starts with a Panic Meter (🟢 Relax. / 🟡 Worth a look. / 🔴 Act now.), at most 3 short bullets, and exactly one next step.

![screenshot placeholder](docs/screenshot.png)

## Architecture

```
Browser (index.html)
   |  GET /  ,  POST /ask {question, demo}
   v
Lambda Function URL  ->  Lambda (Python 3.12, x86_64, 1024 MB, 90 s)
                           |-- handler.py  routing + x-billsense-key check
                           |-- agent.py    Strands Agent + Panic Meter validation
                           |-- tools.py    get_cost_by_service, find_usual_suspects
                                 |-- Cost Explorer (us-east-1, cached 10 min)
                                 |-- EC2 Describe* (ap-south-1, us-east-1)
                           '-- Amazon Bedrock: Nova Pro (apac inference profile)
```

All tools are read-only. `demo=true` uses hardcoded sample data and needs no key or AWS access.

## Deploy (Windows, PowerShell, no Docker)

Do not use `sam build`: it would install Windows wheels that crash on Lambda.

```powershell
.\build.ps1
sam deploy --guided          # region ap-south-1; AccessKey = your owner passcode (12+ chars), AlertEmail = budget alert address
```

`build.ps1` installs Linux wheels into `.build\` and copies `app\*.py` and `index.html`. Prerequisites: AWS CLI v2, SAM CLI, uv, and Bedrock access to Amazon Nova Pro.

Find the model ID with `aws bedrock list-inference-profiles --region ap-south-1` (this project uses `apac.amazon.nova-pro-v1:0`, set via the `MODEL_ID` parameter).

## Security

The Function URL is public. Real-data requests need the `x-billsense-key` header (the owner passcode) matching `ACCESS_KEY` (compared with `hmac.compare_digest`). Demo requests need no key. Errors are logged to CloudWatch; clients only get friendly messages.

Other protections: per-IP limit of 20 questions per 10 minutes (best-effort, per warm container), credential-leak guard (client and server), ID masking, CSP and other security headers, and a $10/month AWS Budget with an 80% email alert (`AlertEmail` parameter).

## Feedback metrics (CloudWatch Logs Insights)

The 👍/👎 buttons send only `{verdict, value, mode}`, never question or answer text. One JSON line is logged per click. Select the function's log group and run:

```
fields @timestamp
| filter event = "feedback"
| stats sum(value = "up") as thumbs_up, sum(value = "down") as thumbs_down, count(*) as total by verdict
```

Average latency by verdict:

```
fields @timestamp
| filter ispresent(latency_ms)
| stats avg(latency_ms) as avg_ms, count(*) as answers by verdict
```

## Local test

```powershell
uv run local_test.py --demo      # sample data
uv run local_test.py             # your real account
```
