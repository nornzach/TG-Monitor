# Contributing / 贡献指南

Thank you for your interest in contributing to TG Monitor Platform!

感谢您对 TG Monitor Platform 的关注！

## Getting Started / 开始贡献

1. Fork the repository
2. Clone your fork
3. Create a feature branch: `git checkout -b feature/your-feature`
4. Make your changes
5. Test thoroughly
6. Commit with a clear message
7. Push and create a Pull Request

## Development Setup / 开发环境

```bash
git clone https://github.com/your-username/tg-monitor-platform.git
cd tg-monitor-platform
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Configure .env with your MySQL credentials
python -m app.cli init-db
python main.py
```

## Code Style / 代码风格

- Python 3.11+ features (type hints, match statements)
- SQLAlchemy 2.0 `mapped_column` style
- All user-facing text in Chinese (UI) / English (code comments, docs)
- No test suite yet — contributions welcome!

## Project Structure / 项目结构

```
app/
├── web.py           # FastAPI routes
├── config.py        # Settings
├── db.py            # Database
├── models.py        # ORM models
├── collector.py     # Background jobs
├── ai_service.py    # AI integration
├── analysis.py      # Aggregation queries
├── text_utils.py    # Text processing
├── templates/       # Jinja2 HTML
└── static/          # CSS
```

## Areas for Contribution / 可贡献领域

- **Testing**: Unit tests, integration tests
- **Features**: Export functionality, notification system, API documentation
- **Performance**: Query optimization, caching
- **UI/UX**: Mobile improvements, accessibility
- **Documentation**: Translations, tutorials

## Reporting Issues / 报告问题

Please use GitHub Issues with:
- Clear description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Environment details (Python version, MySQL version, OS)

## License / 许可证

By contributing, you agree that your contributions will be licensed under the MIT License.
