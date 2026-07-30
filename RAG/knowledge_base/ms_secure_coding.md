# Secure Coding Checklist and Threat Modeling Triggers

*Original summary written for this demo corpus. Not a reproduction of any published standard.*

## Baseline Secure Coding Checklist

Security review is more consistent when it runs against a checklist rather than intuition.
A workable baseline for a code change:

- **Trust boundaries.** Does this change move data across a boundary — network, process,
  privilege level, or tenant? Every crossing needs validation on the receiving side.
- **Input handling.** Is every externally-controlled value validated against a positive
  specification before use, and encoded correctly at each sink?
- **Query and command construction.** Is any interpreter string built by concatenation?
  If so, it must be converted to parameter binding or allowlisted.
- **AuthN/AuthZ.** Does the change alter who may do what? Object-level authorization must
  be re-verified on every request path the change introduces.
- **Secrets.** Are credentials, tokens, and keys sourced from a secret store rather than
  source control or configuration files, and kept out of logs and error messages?
- **Cryptography.** Is the change using vetted library primitives with standard modes,
  rather than novel constructions? Never implement a cipher or a protocol by hand.
- **Error handling.** Does the code fail closed, and do error messages avoid leaking
  internal structure, stack traces, or account existence?
- **Dependencies.** Does the change add a dependency, and has it been checked for known
  vulnerabilities and reasonable maintenance?
- **Logging and audit.** Are security-relevant events (authentication outcome, permission
  denial, privilege change) recorded with enough context to investigate an incident?

## When to Run a Threat Model

Threat modeling every change is not affordable; the value comes from triggering it on the
changes that warrant it. Reliable triggers:

1. **New or modified authentication or session logic**, including token issuance,
   refresh, password reset, and any multi-factor path.
2. **New authorization rules or role definitions**, or any change to how object ownership
   is checked.
3. **A new externally reachable entry point** — a public endpoint, webhook receiver,
   file upload, or message queue consumer.
4. **A change in how data is stored or classified**, especially anything touching
   personal data, payment data, or credentials.
5. **A new trust relationship with a third party**, including a new SDK that receives
   application data.
6. **Changes to cryptographic material handling**: key generation, rotation, storage.

The model itself can be lightweight. Sketch the data flow, mark trust boundaries, and walk
the change against a small set of threat categories: spoofing identity, tampering with
data, repudiation of actions, information disclosure, denial of service, and elevation of
privilege. For each plausible threat, record the existing mitigation or open a tracked
item. The written artifact matters less than the discipline of asking the questions before
the change ships.

## Compounding Risk

Individual findings should be weighed together, not independently. A change that both
alters authentication and introduces a dynamically built query concentrates two
high-severity categories in one deployment, and any defect in either is harder to attribute
during incident response. When categories compound, the correct response is to reduce
blast radius — split the change, gate it, or stage the rollout — rather than to review
harder.
