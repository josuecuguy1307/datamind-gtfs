# Contributor licensing notes

**Status: open question for the maintainers. Nothing here is implemented.** No
contributor license agreement (CLA) or developer certificate of origin (DCO) is in
place, and none was added silently.

## Where things stand

- The public repository has 7 commits, all by one author. No external contribution
  has been merged.
- `CONTRIBUTING.md` says contributions are incorporated under the license in force
  when they are accepted. That is "inbound = outbound": the contributor licenses
  their work to the project under the same terms everyone else gets.

## Why it may matter later

Offering **separate commercial licenses** (section 8 of the LICENSE) for the whole
code base only works if the project holds enough rights in every line. With plain
inbound = outbound, a contributor keeps their copyright and licenses it under the
public license only. The project could then not grant a commercial license that
covers their code, and could not change the license later, without that
contributor's permission.

## Options to weigh before accepting outside contributions

| Option | What it does | Trade-off |
|---|---|---|
| Do nothing (inbound = outbound) | Simplest; contributors keep copyright | No commercial licensing of contributed code without asking each contributor |
| DCO only | Contributor certifies they have the right to submit | Proves origin, but grants no extra rights to relicense |
| CLA with a broad license grant | Contributor grants the project the right to relicense and sublicense their work | Needs a signing step; some contributors object |
| Copyright assignment | Project owns contributions outright | Strongest for dual licensing, hardest to ask of contributors; legal form varies by country |

## Also to check

- **Current rights holders.** The copyright notice names two people. Relicensing
  new versions should have the written agreement of every copyright holder of the
  existing code.
- **Legal entity.** Commercial licenses are usually granted by a company. Decide
  whether the licensor should be a legal entity rather than individuals.
- **Timing.** Settle this before the first outside contribution is merged, because
  adding a CLA later means going back to every earlier contributor.
