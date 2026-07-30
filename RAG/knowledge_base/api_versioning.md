# API Versioning, Compatibility, and Deprecation

*Original summary written for this demo corpus. Not a reproduction of any published standard.*

## What Counts as a Breaking Change

A change is breaking if a client that worked against the previous contract can fail
against the new one. Consumers depend on more of the contract than producers intend, so
the test is behavioral, not intentional.

Breaking, in a request contract: removing or renaming a field or endpoint; making an
optional parameter required; narrowing an accepted type, range, or enum; changing default
values; tightening validation so previously accepted input is now rejected; changing
authentication or required scopes.

Breaking, in a response contract: removing or renaming a field; changing a field's type or
nullability; changing the meaning of an existing value; altering status codes or error
shapes that clients branch on; changing pagination or ordering semantics that clients rely
on implicitly.

Generally safe: adding a new optional request field with a backward-compatible default;
adding a new field to a response, provided clients are documented to ignore unknown fields;
adding a new endpoint; adding a new enum value **only** where clients have a defined
fallback for unrecognized values — otherwise this too is breaking.

Renaming is never a compatible operation. Add the new name, support both, migrate
consumers, then remove the old one on a published schedule.

## Versioning Strategies

Version explicitly rather than letting behavior drift. URI path versioning is the most
visible and the easiest to route and cache. Header or media-type versioning keeps URLs
stable and models the contract more precisely, at the cost of being easier for clients to
get wrong. Whichever is chosen, apply it consistently and document the guarantees each
version carries.

Prefer additive evolution within a version and reserve a new major version for genuinely
incompatible changes; every live version is a maintenance obligation, so minimize how many
exist at once. Run old and new side by side during migration rather than cutting over, so
rollback is a routing decision.

## Deprecation Windows

Deprecation is a process with a schedule, not an announcement. A workable sequence:

1. **Announce** with a documented removal date and a concrete migration path — not merely
   "use v2", but the field-by-field mapping.
2. **Signal in-band**: deprecation response headers and structured warnings so client
   teams see it in their own logs rather than needing to read a changelog.
3. **Measure**: instrument per-consumer usage of the deprecated surface so removal is
   driven by evidence rather than optimism. Contact remaining consumers directly.
4. **Wait**: keep the window proportional to consumer release cycles — internal consumers
   may need a quarter, mobile and third-party clients considerably longer, since old app
   versions persist in the field long after release.
5. **Brown out** before removal: return errors for short scheduled windows to surface
   undiscovered dependencies while rollback is still trivial.
6. **Remove**, and keep the ability to restore quickly if an unmeasured consumer surfaces.

Shipping a breaking API change without a versioning strategy and a deprecation window is
the failure mode this process exists to prevent; when a review flags a breaking change, the
required response is either to make it additive or to attach it to an explicit version and
schedule.
