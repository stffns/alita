## Summary

<!-- One paragraph: what changes and why. Focus on the why -- the what
shows in the diff. -->

## Related

<!-- Closes #N when this PR closes an issue. Refs #N for partial work.
Write "no issue" if there is no issue (e.g. small refactors). -->

## Test plan

- [ ] `ruff check pelops tests` silent
- [ ] `ruff format --check pelops tests` silent
- [ ] `python -m pytest -q` green
- [ ] `pre-commit run --all-files` green
- [ ] (for behavior changes) `pelops-doctor` reports all checks green
- [ ] (for new tools, persona, or skills) manual smoke via Telegram or Chainlit

## Notes for reviewer

<!-- Anything tricky, deliberate, or worth flagging. Architectural
choices that future-Jay should remember the reasoning for. -->
