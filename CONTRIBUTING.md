# Contributing to Remote Desktop

Thank you for your interest in contributing! Remote Desktop is an open-source, self-hosted remote access and streaming platform.

## Code of Conduct

Please be respectful, constructive, and mindful of security implications in all interactions and contributions.

## Development Workflow

1. **Fork and Clone**
   ```bash
   git clone https://github.com/YOUR_USERNAME/remote-desktop.git
   cd remote-desktop
   ```

2. **Set Up Python Virtual Environment**
   ```bash
   python -m venv venv
   # Windows PowerShell:
   .\venv\Scripts\Activate.ps1
   # Linux/macOS:
   source venv/bin/activate

   pip install -r requirements.txt
   ```

3. **Configure Environment**
   ```bash
   copy .env.example .env
   # Generate a local test secret:
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

4. **Branching Model**
   - Create a dedicated feature branch from `main`:
     ```bash
     git checkout -b feature/your-feature-name
     ```

5. **Run the Test Suite**
   Ensure all unit and security regression tests pass:
   ```bash
   python -m pytest tests/ -v
   ```

## Contribution & Security Rules

- **Zero Secret Commits:** Never commit `.env`, `*.db`, certificates (`*.pem`, `*.key`), session tokens, recordings, or user uploads.
- **Maintain Defensive Defaults:** The viewer must always default to `Screen View` mode. Remote input injection must be gated behind explicit server-side state.
- **Input Sanitization:** Any new keyboard shortcut, mouse action, or system command must be validated against the security sanitizer.
- **Audit Logging:** Security-sensitive actions (auth events, power operations, mode changes) must emit structured audit events without logging sensitive data (passwords, tokens, clipboard content).

## Pull Request Guidelines

- Provide a clear description of the feature or bug fix.
- Include unit/integration tests in `tests/` for new endpoints or security behaviors.
- Ensure automated tests pass before submitting.
