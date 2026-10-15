# Pull Request Checklist

## 📝 Description
(Describe what this PR changes. If this is a new agent, describe its goal.)

## 🔗 Related Issues
Fixes # (issue)

## ✅ Checklist
- [ ] **Code Quality**:
    - [ ] My code follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).
    - [ ] formatters (`black`, `isort`) have been run.
    - [ ] Type hints needed for public methods are present.
- [ ] **Research Observability**:
    - [ ] (If applicable) The agent/SLM logs its reasoning trace clearly.
    - [ ] No "magic numbers" hardcoded; configs are exposed.
- [ ] **Testing**:
    - [ ] Unit tests added for new tools/functions.
    - [ ] (For experiments) A reproduction script or README is included.

## 🧪 Experiment Results (Optional)
If this PR improves an agent's performance, briefly state the improvement (e.g., "Success rate increased from 80% to 92%").
