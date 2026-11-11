//! Live provider discovery. A catalog row is not a claim of account entitlement
//! or image support: those are reported separately and checked before activation.
use crate::{config::Config, credentials};
use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub const RANK_SOURCE: &str = "https://arena.ai/leaderboard/chat/vision";
pub const RANK_DATE: &str = "2026-11-06";

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Preset {
    pub id: String,
    pub name: String,
    pub base_url: String,
    pub kind: String,
    pub models_url: String,
    pub key_url: String,
    pub schema_mode: String,
    pub vision_score: Option<u32>,
    pub ranked_model: String,
}
pub fn providers() -> Vec<Preset> {
    // Overall Vision Arena point estimates, best listed model from each lab.
    // Confidence intervals overlap; these are dated sorting hints, not claims
    // that a lab or a newly released model is universally better. Gateways and
    // providers without comparable evidence remain explicitly unranked.
    let rows = [
        (
            "anthropic",
            "Anthropic",
            "https://api.anthropic.com/v1",
            "anthropic",
            "",
            "https://platform.claude.com/settings/keys",
            "schema",
            1309,
            "Claude Fable 5 (High)",
        ),
        (
            "qwen",
            "Alibaba Qwen · International",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "chat",
            "",
            "https://modelstudio.console.alibabacloud.com/",
            "json",
            1300,
            "Qwen3.8-Max",
        ),
        (
            "google",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "chat",
            "",
            "https://aistudio.google.com/apikey",
            "schema",
            1294,
            "Gemini 3.7 Flash (High)",
        ),
        (
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            "responses",
            "",
            "https://platform.openai.com/api-keys",
            "schema",
            1293,
            "GPT-6.1 Sol (Max)",
        ),
        (
            "xai",
            "xAI / Grok",
            "https://api.x.ai/v1",
            "chat",
            "https://api.x.ai/v1/language-models",
            "https://console.x.ai/",
            "schema",
            1281,
            "Grok 4.5",
        ),
        (
            "zai",
            "Z.ai / GLM",
            "https://api.z.ai/api/paas/v4",
            "chat",
            "",
            "https://z.ai/manage-apikey/apikey-list",
            "json",
            1272,
            "GLM-5.3-Flash",
        ),
        (
            "moonshot",
            "Moonshot / Kimi",
            "https://api.moonshot.ai/v1",
            "chat",
            "",
            "https://platform.moonshot.ai/console/api-keys",
            "json",
            1264,
            "Kimi K2.6",
        ),
        (
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com",
            "chat",
            "",
            "https://platform.deepseek.com/api_keys",
            "json",
            1263,
            "DeepSeek-V4.1-Flash-20260910 (Max)",
        ),
        (
            "minimax",
            "MiniMax",
            "https://api.minimax.io/v1",
            "chat",
            "",
            "https://platform.minimax.io/",
            "json",
            1231,
            "MiniMax-M3",
        ),
        (
            "mistral",
            "Mistral",
            "https://api.mistral.ai/v1",
            "chat",
            "",
            "https://console.mistral.ai/api-keys",
            "schema",
            1230,
            "Mistral Large 4",
        ),
        (
            "openrouter",
            "OpenRouter · model gateway",
            "https://openrouter.ai/api/v1",
            "chat",
            "",
            "https://openrouter.ai/settings/keys",
            "schema",
            0,
            "",
        ),
        (
            "together",
            "Together AI",
            "https://api.together.xyz/v1",
            "chat",
            "",
            "https://api.together.ai/settings/api-keys",
            "schema",
            0,
            "",
        ),
        (
            "fireworks",
            "Fireworks AI",
            "https://api.fireworks.ai/inference/v1",
            "chat",
            "https://api.fireworks.ai/v1/accounts/fireworks/models",
            "https://fireworks.ai/account/api-keys",
            "schema",
            0,
            "",
        ),
        (
            "groq",
            "Groq",
            "https://api.groq.com/openai/v1",
            "chat",
            "",
            "https://console.groq.com/keys",
            "json",
            0,
            "",
        ),
        (
            "nvidia",
            "NVIDIA NIM",
            "https://integrate.api.nvidia.com/v1",
            "chat",
            "",
            "https://build.nvidia.com/",
            "json",
            0,
            "",
        ),
        (
            "huggingface",
            "Hugging Face · inference gateway",
            "https://router.huggingface.co/v1",
            "chat",
            "",
            "https://huggingface.co/settings/tokens",
            "schema",
            0,
            "",
        ),
        (
            "nebius",
            "Nebius Token Factory",
            "https://api.tokenfactory.nebius.com/v1",
            "chat",
            "",
            "https://tokenfactory.nebius.com/",
            "schema",
            0,
            "",
        ),
        (
            "novita",
            "Novita AI",
            "https://api.novita.ai/openai",
            "chat",
            "",
            "https://novita.ai/console/key-management",
            "json",
            0,
            "",
        ),
        (
            "siliconflow",
            "SiliconFlow · International",
            "https://api.siliconflow.com/v1",
            "chat",
            "",
            "https://cloud.siliconflow.com/account/ak",
            "json",
            0,
            "",
        ),
        (
            "sambanova",
            "SambaNova",
            "https://api.sambanova.ai/v1",
            "chat",
            "",
            "https://cloud.sambanova.ai/",
            "json",
            0,
            "",
        ),
        (
            "ollama",
            "Ollama · local",
            "http://localhost:11434/v1",
            "chat",
            "",
            "https://ollama.com/",
            "schema",
            0,
            "",
        ),
        (
            "lmstudio",
            "LM Studio · local",
            "http://localhost:1234/v1",
            "chat",
            "",
            "https://lmstudio.ai/",
            "schema",
            0,
            "",
        ),
        (
            "custom",
            "Custom compatible endpoint",
            "http://localhost:8000/v1",
            "chat",
            "",
            "",
            "schema",
            0,
            "",
        ),
    ];
    rows.into_iter()
        .map(
            |(id, name, url, kind, models, key, schema, score, ranked)| Preset {
                id: id.into(),
                name: name.into(),
                base_url: url.into(),
                kind: kind.into(),
                models_url: if models.is_empty() {
                    format!("{url}/models")
                } else {
                    models.into()
                },
                key_url: key.into(),
                schema_mode: schema.into(),
                vision_score: (score > 0).then_some(score),
                ranked_model: ranked.into(),
            },
        )
        .collect()
}
impl Preset {
    pub fn apply(&self, config: &mut Config) {
        if config.provider.base_url.trim_end_matches('/') != self.base_url {
            config.provider.credential = None;
            config.provider.model.clear();
            config.provider.vision_model.clear();
        }
        config.provider.catalog_id = self.id.clone();
        config.provider.base_url = self.base_url.clone();
        config.provider.kind = self.kind.clone();
        config.provider.structured_output = self.schema_mode.clone();
        config.provider.key_env = match self.id.as_str() {
            "openai" => "OPENAI_API_KEY",
            "anthropic" => "ANTHROPIC_API_KEY",
            _ => "",
        }
        .into();
        config.provider.prompt_cache = match self.kind.as_str() {
            "anthropic" => "ephemeral",
            "responses" => "implicit",
            _ => "off",
        }
        .into();
        config.provider.input_price_per_million = None;
        config.provider.cached_input_price_per_million = None;
        config.provider.output_price_per_million = None;
        config.provider.vision_input_price_per_million = None;
        config.provider.vision_cached_input_price_per_million = None;
        config.provider.vision_output_price_per_million = None;
        config.processing.max_cost = None;
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub name: String,
    pub vision: Option<bool>,
    pub context: Option<usize>,
    pub max_output: Option<usize>,
    pub input_price: Option<f64>,
    pub output_price: Option<f64>,
    pub cached_price: Option<f64>,
}
pub fn local(config: &Config) -> bool {
    reqwest::Url::parse(&config.provider.base_url)
        .ok()
        .and_then(|u| u.host_str().map(str::to_owned))
        .is_some_and(|h| matches!(h.as_str(), "localhost" | "127.0.0.1" | "::1" | "[::1]"))
}
fn number(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str()?.parse().ok())
        .filter(|v| v.is_finite() && *v >= 0.0)
}
fn modality(value: &Value) -> Option<bool> {
    value.as_array().map(|a| {
        a.iter()
            .any(|v| v.as_str().is_some_and(|s| s.eq_ignore_ascii_case("image")))
    })
}
fn parse_model(value: &Value, provider: &str) -> Option<Model> {
    let id = value["id"]
        .as_str()
        .or_else(|| value["name"].as_str())?
        .to_owned();
    if id.len() > 512 || id.chars().any(char::is_control) {
        return None;
    }
    if value["active"].as_bool() == Some(false)
        || value["type"]
            .as_str()
            .is_some_and(|t| matches!(t, "embedding" | "image" | "audio" | "moderation" | "rerank"))
    {
        return None;
    }
    let vision = modality(&value["input_modalities"])
        .or_else(|| modality(&value["architecture"]["input_modalities"]))
        .or_else(|| modality(&value["inputModalities"]))
        .or_else(|| value["capabilities"]["vision"].as_bool())
        .or_else(|| value["supportsImageInput"].as_bool());
    if modality(&value["output_modalities"]) == Some(true)
        && !value["output_modalities"]
            .as_array()
            .is_some_and(|a| a.iter().any(|v| v.as_str() == Some("text")))
    {
        return None;
    }
    let mut model = Model {
        id: id.clone(),
        name: value["display_name"]
            .as_str()
            .or_else(|| value["displayName"].as_str())
            .or_else(|| value["name"].as_str())
            .unwrap_or(&id)
            .chars()
            .filter(|c| !c.is_control())
            .take(200)
            .collect(),
        vision,
        context: value["context_window"]
            .as_u64()
            .or_else(|| value["context_length"].as_u64())
            .or_else(|| value["max_model_len"].as_u64())
            .or_else(|| value["context_size"].as_u64())
            .map(|v| v as usize),
        max_output: value["max_output_tokens"].as_u64().map(|v| v as usize),
        input_price: None,
        output_price: None,
        cached_price: None,
    };
    if provider == "openrouter" {
        model.input_price = number(&value["pricing"]["prompt"]).map(|v| v * 1_000_000.0);
        model.output_price = number(&value["pricing"]["completion"]).map(|v| v * 1_000_000.0);
        model.cached_price = number(&value["pricing"]["input_cache_read"]).map(|v| v * 1_000_000.0);
    } else if provider == "xai" {
        model.input_price = number(&value["prompt_text_token_price"]).map(|v| v / 10_000.0);
        model.output_price = number(&value["completion_text_token_price"]).map(|v| v / 10_000.0);
        model.cached_price = number(&value["cached_prompt_text_token_price"]).map(|v| v / 10_000.0);
    } else if provider == "anthropic" && id == "claude-haiku-5-5" {
        // Official 2026-11-07 short-context rates; the setup profile stays below
        // the 100K-token tier. No rates are invented for other model identifiers.
        model.input_price = Some(0.10);
        model.output_price = Some(0.50);
        model.cached_price = Some(0.01);
    }
    Some(model)
}
pub async fn discover(config: &Config) -> Result<Vec<Model>> {
    config.validate()?;
    let provider = config.provider.clone();
    let key = tokio::task::spawn_blocking(move || credentials::resolve(&provider)).await??;
    if key.is_none() && !local(config) {
        bail!("Connect an API key before retrieving models");
    }
    let p = &config.provider;
    let preset = providers().into_iter().find(|v| {
        v.id == p.catalog_id && v.base_url.trim_end_matches('/') == p.base_url.trim_end_matches('/')
    });
    let endpoint = preset
        .map(|v| v.models_url)
        .unwrap_or_else(|| format!("{}/models", p.base_url.trim_end_matches('/')));
    let http = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(45))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let mut result = BTreeMap::new();
    let mut cursor = String::new();
    let mut seen = std::collections::BTreeSet::new();
    for page in 0..100 {
        let mut request = http.get(&endpoint);
        if let Some(key) = &key {
            request = if p.kind == "anthropic" {
                request
                    .header("x-api-key", key.as_str())
                    .header("anthropic-version", "2023-06-01")
            } else {
                request.bearer_auth(key.as_str())
            };
        }
        if p.catalog_id == "nebius" {
            request = request.query(&[("verbose", "true")]);
        }
        if p.kind == "anthropic" {
            request = request.query(&[("limit", "100")]);
            if !cursor.is_empty() {
                request = request.query(&[("after_id", cursor.as_str())]);
            }
        } else if !cursor.is_empty() {
            request = request.query(&[("pageToken", cursor.as_str())]);
        }
        let mut response = request
            .send()
            .await
            .context("Could not reach the provider's model catalog")?;
        if !response.status().is_success() {
            bail!(
                "Model discovery returned HTTP {}. Check the key, endpoint and account access. No static model list was substituted.",
                response.status().as_u16()
            );
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            if bytes.len() + chunk.len() > 16 * 1024 * 1024 {
                bail!("Provider model catalog exceeded 16 MiB");
            }
            bytes.extend_from_slice(&chunk);
        }
        let body: Value = serde_json::from_slice(&bytes)
            .context("Provider did not return a JSON model catalog")?;
        let rows = body
            .as_array()
            .or_else(|| body["data"].as_array())
            .or_else(|| body["models"].as_array())
            .context("Provider model-list response has an unsupported format")?;
        for row in rows {
            if let Some(model) = parse_model(row, &p.catalog_id) {
                result.insert(model.id.clone(), model);
            }
        }
        if result.len() > 100_000 {
            bail!("Provider returned too many models");
        }
        let next = if body["has_more"].as_bool() == Some(true) {
            body["last_id"].as_str()
        } else {
            body["nextPageToken"].as_str()
        };
        match next.filter(|v| !v.is_empty()) {
            Some(next) => {
                if page == 99 || !seen.insert(next.to_owned()) {
                    bail!("Model catalog pagination did not finish");
                }
                cursor = next.into();
            }
            None => break,
        }
    }
    if result.is_empty() {
        bail!("The provider returned no available models for this key");
    }
    let mut models = result.into_values().collect::<Vec<_>>();
    models.sort_by_key(|m| {
        (
            m.vision == Some(false),
            m.vision.is_none(),
            m.name.to_lowercase(),
        )
    });
    Ok(models)
}

pub fn select(config: &mut Config, model: &Model) -> Result<()> {
    if model.context.is_some_and(|v| v < 16384) || model.max_output.is_some_and(|v| v < 1024) {
        bail!(
            "This model reports limits too small for the document pipeline. Choose a model with at least 16,384 context tokens and 1,024 output tokens."
        );
    }
    config.provider.model = model.id.clone();
    config.provider.vision_model = model.id.clone();
    // Keep bounded page requests even when the model advertises a huge context.
    config.provider.context_tokens = model.context.unwrap_or(32768).min(32768);
    config.provider.max_output_tokens = model
        .max_output
        .unwrap_or(8192)
        .clamp(256, 8192)
        .min(config.provider.context_tokens / 4);
    config.provider.input_price_per_million = model.input_price;
    config.provider.output_price_per_million = model.output_price;
    config.provider.cached_input_price_per_million = model.cached_price;
    config.provider.vision_input_price_per_million = None;
    config.provider.vision_output_price_per_million = None;
    config.provider.vision_cached_input_price_per_million = None;
    if model.input_price.is_none() || model.output_price.is_none() {
        config.processing.max_cost = None;
    }
    config.validate()?;
    Ok(())
}

/// Real protocol/credential/vision check, invoked only by an explicit setup action.
/// This establishes that the selected model sees an image and returns usable
/// structured data. It does not measure OCR or document-processing quality.
pub async fn verify(config: &Config, root: &std::path::Path) -> Result<Value> {
    use crate::{
        provider::{ModelClient, Request, object_schema},
        store::Library,
    };
    use serde_json::json;
    let scratch = tempfile::tempdir_in(root)?;
    let library = Library::open(scratch.path())?;
    let mut probe = config.clone();
    probe.provider.attempts = 1;
    probe.provider.concurrency = 1;
    probe.provider.max_output_tokens = 1024;
    probe.processing.max_requests = 1;
    probe.processing.max_generated_tokens = 1024;
    let job = library.enqueue(
        std::path::Path::new("setup-connection-check"),
        scratch.path(),
        &probe,
    )?;
    library.start(job)?;
    let colors = [
        ("red", [255u8, 0, 0]),
        ("green", [0, 180, 0]),
        ("blue", [0, 0, 255]),
        ("yellow", [255, 255, 0]),
    ];
    let nonce = blake3::hash(format!("{:?}", std::time::SystemTime::now()).as_bytes());
    let mut order = [0usize, 1, 2, 3];
    for i in (1..4).rev() {
        order.swap(i, nonce.as_bytes()[i] as usize % (i + 1));
    }
    let bitmap = image::RgbImage::from_fn(128, 128, |x, y| {
        image::Rgb(colors[order[((y / 64) * 2 + x / 64) as usize]].1)
    });
    let mut png = std::io::Cursor::new(Vec::new());
    image::DynamicImage::ImageRgb8(bitmap).write_to(&mut png, image::ImageFormat::Png)?;
    let schema = object_schema(
        json!({"colors":{"type":"array","items":{"type":"string","enum":["red","green","blue","yellow"]}}}),
        &["colors"],
    );
    let expected = order.iter().map(|i| colors[*i].0).collect::<Vec<_>>();
    let client = ModelClient::new(&probe)?;
    client.request(&library,job,Request { context: None,task:"setup_vision_check",instructions:"Identify the solid color of each of the four equal squares in this image. Return JSON with colors in this order: top left, top right, bottom left, bottom right. Do not guess from the instructions; use the image.",input:json!({"operation":"connection_check"}),schema,image:Some((png.get_ref(),"image/png")),validate:&|value| {
        let actual=value["colors"].as_array().context("Missing colors")?.iter().filter_map(Value::as_str).collect::<Vec<_>>();
        if actual!=expected {bail!("The model's image response did not match the connection-check image. Choose a different model or retry.");}Ok(())
    }}).await?;
    let usage = library.job(job)?;
    Ok(
        json!({"model":config.provider.model,"image_input_and_structured_output":true,"input_tokens":usage.input_tokens,"output_tokens":usage.output_tokens,"estimated_cost":usage.cost}),
    )
}
