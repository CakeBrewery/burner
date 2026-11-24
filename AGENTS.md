# burner agent notes

Read `README.md` and `CONTRIBUTING.md` first.

## CodeRabbit reviews
- CodeRabbit reviews a pull request only when it carries the label
  `coderabbit-review`. Without the label no review comes, so do not wait for
  one. A passing or skipped CodeRabbit check is not a review. A merge gate
  that needs a CodeRabbit review applies only to a labeled pull request.
- Add the label to a pull request into `main` that changes security,
  authentication, stored data, a migration, or a protocol, or when the owner
  asks: `gh pr edit <number> --add-label coderabbit-review`.
- The review covers the commits on the branch when the label goes on. Later
  pushes get no review. Ask the owner before you request another one, and do
  not post `@coderabbitai review` comments.
- All repositories share one weekly review allowance. When CodeRabbit answers
  "Review rate limited", wait. Do not add the label again or comment.
- `.coderabbit.yaml` holds this repository's settings. It takes the shared
  base from the private `CakeBrewery/coderabbit` repository, where shared
  settings change.
