"""Identity and authorization primitives.

The runtime contract: an incoming request is parsed exactly once at the edge
into an immutable `PrincipalContext`. Downstream code consumes that context;
no component reloads identity itself. Splitting this from `app.core` is
deliberate — auth is a real subsystem with its own data and tests, not a
couple of helpers next to the settings module.
"""