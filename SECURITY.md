# Security

Steward exists to stop an AI from changing your computer without your OK, so a way around that is the most serious kind of bug. This includes:

- running a change without approval, or with an approval for something different
- escaping the read-only sandbox
- reaching a protected or secret path
- a plan doing more than the approval showed
- ZeroClaw reaching your system without going through Steward
- a secret ending up in a log, a process argument or a model prompt

Please report these privately through GitHub's **Report a vulnerability** button on the Security tab of this repository. Don't open a public issue.

Include:

- what you ran
- what happened, and what should have happened
- your distribution and version
- the output of `steward doctor` (it doesn't print secrets)

Known limits are listed under "Not tested" and "Residual risks" in [steward/TEST-REPORT.md](steward/TEST-REPORT.md).
