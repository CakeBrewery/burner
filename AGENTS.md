# burner agent notes

Read `README.md` and `CONTRIBUTING.md` first.

## CodeRabbit reviews
- CodeRabbit reviews a pull request only when it carries the label
  `coderabbit-review`. Without the label no review comes, so do not wait for
  one. A merge gate that needs a CodeRabbit review applies only to a labeled
  pull request.
- Add the label whenever you want a review:
  `gh pr edit <number> --add-label coderabbit-review`. Do not post
  `@coderabbitai review` comments.
- The review covers the commits on the branch when the label goes on. Later
  pushes get no review.
- When CodeRabbit answers "Review rate limited", wait for the limit to reset.
- `.coderabbit.yaml` holds this repository's settings. It takes the shared
  base from the private `CakeBrewery/coderabbit` repository, where shared
  settings change.
