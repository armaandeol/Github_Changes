# Web Application Security Essentials

*Original summary written for this demo corpus. Not a reproduction of any published standard.*

## Injection Prevention

Injection flaws occur whenever untrusted input is concatenated into an interpreter's
command string — SQL, shell, LDAP, XPath, or a template engine. The defect is not the
untrusted data itself but the loss of the boundary between code and data. String
concatenation and string interpolation destroy that boundary; parameter binding
preserves it.

The primary defense is parameterized queries (prepared statements). The query text is
sent to the database once, with placeholders, and the values are bound separately, so
the engine never re-parses user input as syntax. Object-relational mappers give this
for free as long as raw-SQL escape hatches are avoided. When a query fragment must
genuinely be dynamic — a sortable column name, a table name — parameter binding does
not apply; instead validate the fragment against an explicit allowlist of permitted
values and reject anything else. Manual escaping is a last resort and is easy to get
wrong across character sets.

Secondary defenses layer on top: run database accounts with the narrowest privileges
the application needs, so a successful injection yields less. Prefer stored logic that
does not build dynamic SQL internally. Add automated tests that assert the query is
parameterized, not merely that it returns correct rows for benign input — a
concatenated query passes the happy-path test just as well as a safe one.

Any change that touches query construction deserves explicit review attention even when
the diff looks small, because injection defects are single-line defects.

## Authentication and Session Handling

Authentication changes are disproportionately risky: the failure mode is silent, and a
regression grants access rather than denying it. Store passwords only as salted hashes
from a deliberately slow, memory-hard algorithm; never store recoverable credentials.
Enforce rate limiting and progressive backoff on login endpoints so credential-stuffing
is expensive. Keep failure messages generic so they do not reveal whether an account
exists.

Session identifiers must be long, random, and regenerated at every privilege
transition — most importantly immediately after successful login, which defeats session
fixation. Cookies carrying session state should be marked HttpOnly, Secure, and given an
appropriate SameSite policy. Sessions need both an idle timeout and an absolute lifetime,
and logout must invalidate server-side state rather than only clearing the client cookie.

Multi-factor authentication meaningfully reduces account takeover. When adding it, the
recovery and fallback paths deserve as much scrutiny as the primary path, since attackers
target the weakest branch.

Treat authorization as separate from authentication. Verify on every request that the
authenticated principal may act on the specific object being addressed; missing
object-level checks are among the most common and most exploitable defects.

## Input Validation and Output Handling

Validate input at the trust boundary, against a positive specification: expected type,
length, range, format, and permitted character set. Denylists of "bad" patterns fail
because attackers enumerate encodings the list did not anticipate. Canonicalize before
validating, or a validated string may still decode into something dangerous downstream.

Validation is not a substitute for context-correct output encoding. The same value is
safe in one sink and dangerous in another, so encode at the point of use — HTML body,
HTML attribute, JavaScript, URL, and SQL each require different treatment.

Fail closed. When validation cannot decide, reject. Log the rejection with enough context
to investigate, but never log the raw secret or credential involved.
