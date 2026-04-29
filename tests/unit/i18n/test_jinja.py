from __future__ import annotations

from jinja2 import DictLoader, Environment

from app.domain.messaging.notifications import Jinja2TemplateLoader
from app.i18n import PSEUDO_LOCALE, install_jinja_i18n


def test_jinja_trans_block_uses_catalog() -> None:
    env = Environment(
        loader=DictLoader({"subject.j2": "{% trans %}login.title{% endtrans %}"}),
        extensions=["jinja2.ext.i18n"],
    )
    install_jinja_i18n(env)

    assert env.get_template("subject.j2").render() == "Sign in to crew.day"


def test_template_loader_uses_render_locale_for_trans_blocks() -> None:
    env = Environment(
        loader=DictLoader({"probe.subject.j2": "{% trans %}login.title{% endtrans %}"}),
        extensions=["jinja2.ext.i18n"],
    )
    install_jinja_i18n(env)
    loader = Jinja2TemplateLoader(env=env)

    out = loader.render(
        kind="probe",
        locale=PSEUDO_LOCALE,
        channel="subject",
        context={},
    )

    assert out.startswith("[!! ")
    assert "Sígn" in out


def test_notification_template_render_is_unchanged_after_i18n_extension() -> None:
    out = Jinja2TemplateLoader.default().render(
        kind="task_assigned",
        locale=None,
        channel="subject",
        context={"task_title": "Clean room 3"},
    )

    assert out == "Task assigned: Clean room 3"
