# Rules you give once

[Contexto](../README.md) / [Documentation](README.md)

What happens to a rule you state once ("from now on, ...") when its turn is paged out, and how to
make it stay. The terms used here (paged out, pinned state, page index) are defined in the README's
[How it works](../README.md#how-it-works).

When turns are paged out, the model keeps seeing the pinned state and the page index, which holds
the opening of each paged-out turn (about its first 200 characters). A standing rule ("from now on,
write dates in ISO format") stated at the start of a message stays in view through that index. A
rule given in the middle or at the end of a longer message is gone from view once its turn is paged
out, unless the model pinned it with `contexto_pin`. The model is told to pin a standing rule or
preference as an obligation as soon as you state it, and to keep it open until you withdraw it. In
a live check with that wording the model pinned and applied 8 of 8 rules (it pinned 0 of 4 before
it), but that was one model and 8 rules. A rule the model did not pin leaves view once its turn is
paged out; its words stay searchable, but the model rarely searches for a rule it does not know
about.

If a rule matters:

- state it at the start of its own message, or
- ask the model to pin it ("pin that as an obligation"), and
- check with `contexto state`, which prints the pinned state.
