import textwrap
import loxo_llm_router.config as C


def _write(tmp_path, body):
    p = tmp_path / "loxo.toml"
    p.write_text(textwrap.dedent(body))
    return p


def test_defaults_load_loxo_namespace_and_tiers(monkeypatch):
    monkeypatch.delenv("LOXO_CONFIG", raising=False)
    monkeypatch.chdir("/")  # no ./loxo.toml
    cfg = C.load_config()
    assert cfg.namespace == "loxo"
    assert set(cfg.tiers) >= {
        "loxo/auto", "loxo/fast", "loxo/balanced",
        "loxo/reason", "loxo/deep", "loxo/local",
    }
    assert cfg.tiers["loxo/auto"].routing == "auto"
    assert cfg.tiers["loxo/auto"].cloud_target == "z-ai/glm-5.2"
    assert cfg.tiers["loxo/balanced"].vision == "shim"     # not reject
    assert cfg.tiers["loxo/reason"].vision == "native"     # not reject
    assert cfg.tiers["loxo/fast"].advertised_context == 202752
    assert cfg.tiers["loxo/reason"].advertised_context == 262144
    assert cfg.tiers["loxo/local"].routing == "local"
    assert cfg.tiers["loxo/local"].cloud_target is None
    # local tier's context window is no longer defaulted at load time (B3):
    # None means "derive at serve time" (the local limit, via _virtual_model_entries).
    assert cfg.tiers["loxo/local"].advertised_context is None


def test_toml_file_overrides_defaults(tmp_path, monkeypatch):
    cfg_path = _write(tmp_path, """
        namespace = "acme"
        [backends]
        local_context_limit = 12345
        [tiers.auto]
        routing = "auto"
        cloud_target = "vendor/model-x"
        vision = "shim"
    """)
    monkeypatch.setenv("LOXO_CONFIG", str(cfg_path))
    cfg = C.load_config()
    assert cfg.namespace == "acme"
    assert cfg.local_context_limit == 12345
    assert cfg.tiers["acme/auto"].cloud_target == "vendor/model-x"


def test_env_overrides_toml(tmp_path, monkeypatch):
    cfg_path = _write(tmp_path, 'namespace = "acme"\n[server]\nport = 9090\n')
    monkeypatch.setenv("LOXO_CONFIG", str(cfg_path))
    monkeypatch.setenv("ROUTER_NS", "zeta")
    monkeypatch.setenv("PORT", "9191")
    cfg = C.load_config()
    assert cfg.namespace == "zeta"      # env beats file
    assert cfg.port == 9191             # bare PORT honored (PaaS convention)
    assert "zeta/auto" in cfg.tiers     # ids follow the namespace


def test_loxo_port_beats_bare_port(monkeypatch):
    # Namespaced override wins over a (possibly inherited) bare PORT.
    monkeypatch.delenv("LOXO_CONFIG", raising=False)
    monkeypatch.chdir("/")
    monkeypatch.setenv("PORT", "8080")        # e.g. platform-injected
    monkeypatch.setenv("LOXO_PORT", "9595")   # escape hatch
    cfg = C.load_config()
    assert cfg.port == 9595


def test_secrets_come_only_from_env(tmp_path, monkeypatch):
    # API key in the TOML must be ignored; env is the only source.
    cfg_path = _write(tmp_path, 'openrouter_api_key = "leaked-from-file"\n')
    monkeypatch.setenv("LOXO_CONFIG", str(cfg_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    cfg = C.load_config()
    assert cfg.openrouter_api_key == "sk-from-env"


def test_partial_tier_override_preserves_other_fields(tmp_path, monkeypatch):
    # A user specifying only cloud_target should keep bundled vision and advertised_context.
    cfg_path = _write(tmp_path, """
        [tiers.deep]
        routing = "cloud"
        cloud_target = "my-org/better-model"
    """)
    monkeypatch.setenv("LOXO_CONFIG", str(cfg_path))
    cfg = C.load_config()
    deep = cfg.tiers["loxo/deep"]
    assert deep.cloud_target == "my-org/better-model"   # override applied
    assert deep.vision == "native"                       # bundled value preserved
    assert deep.advertised_context == 1048576            # bundled value preserved


def test_local_models_env_is_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("LOXO_CONFIG", str(_write(tmp_path, "namespace='loxo'\n")))
    monkeypatch.setenv("LOCAL_MODELS", "a-model, b-model ,")
    cfg = C.load_config()
    assert cfg.local_models == ("a-model", "b-model")
