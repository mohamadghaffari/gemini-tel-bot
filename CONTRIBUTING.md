## CI/CD Setup

This project uses GitHub Actions for Continuous Integration and Continuous Deployment (CI/CD).

### Workflows

*   **MyPy Type Check:** This workflow (`.github/workflows/mypy.yml`) runs MyPy to perform static type checking on the `src` directory. It helps to catch type-related errors early in the development process. The status badge is displayed below.

    [![mypy](https://github.com/mohamadghaffari/gemini-tel-bot/actions/workflows/mypy.yml/badge.svg)](https://github.com/mohamadghaffari/gemini-tel-bot/actions/workflows/mypy.yml)

*   **UV Integration:** This workflow (`.github/workflows/uv-integration.yml`) installs uv and sets up Python. The status badge is displayed below.

    [![UV Integration](https://github.com/mohamadghaffari/gemini-tel-bot/actions/workflows/uv-integration.yml/badge.svg)](https://github.com/mohamadghaffari/gemini-tel-bot/actions/workflows/uv-integration.yml)

*   **Black Linting:** This workflow (`.github/workflows/lint-black.yml`) runs Black to lint the code. The status badge is displayed below.

    [![lint](https://github.com/mohamadghaffari/gemini-tel-bot/actions/workflows/lint-black.yml/badge.svg)](https://github.com/mohamadghaffari/gemini-tel-bot/actions/workflows/lint-black.yml)

To view the status of the workflows, click on the badges above or visit the "Actions" tab in the GitHub repository.
