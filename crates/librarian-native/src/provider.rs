use crate::{
    config::Config,
    store::{Library, now},
};
use anyhow::{Context, Result, bail};
use base64::Engine;
use serde_json::{Value, json};
use std::{
    borrow::Cow,
    collections::{HashMap, VecDeque},
    sync::{Arc, OnceLock, Weak},
    time::{Duration, Instant},
};
use tokio::sync::Mutex;

#[derive(Clone)]
pub struct ModelClient {
    http: reqwest::Client,
    config: Config,
    rate: Arc<Mutex<Rate>>,
    pending: Arc<Mutex<HashMap<String, Weak<Mutex<()>>>>>,
    slots: Arc<tokio::sync::Semaphore>,
}
struct Rate {
    next: Instant,
    tokens: VecDeque<(Instant, u64)>,
}
type RatePools = std::sync::Mutex<HashMap<String, Arc<Mutex<Rate>>>>;
static RATE_POOLS: OnceLock<RatePools> = OnceLock::new();
static HTTP_POOLS: OnceLock<std::sync::Mutex<HashMap<u64, reqwest::Client>>> = OnceLock::new();

pub struct Request<'a> {
    pub task: &'a str,
    pub instructions: &'a str,
    pub context: Option<&'a str>,
    pub input: Value,
    pub schema: Value,
    pub image: Option<(&'a [u8], &'a str)>,
    pub validate: &'a dyn Fn(&Value) -> Result<()>,
}

// Three MiB becomes four MiB after base64, leaving room below common gateway
// per-image limits. Keep compact originals lossless; reduce large photos first
// through JPEG and only then through dimensions. Source-page assets stay intact.
fn transport_image<'a>(bytes: &'a [u8], mime: &'a str) -> Result<(Cow<'a, [u8]>, &'a str)> {
    const MAX_BYTES: usize = 3 * 1024 * 1024;
    const MAX_SIDE: u32 = 7680;
    let (width, height) = image::ImageReader::new(std::io::Cursor::new(bytes))
        .with_guessed_format()?
        .into_dimensions()?;
    if bytes.len() <= MAX_BYTES && width.max(height) <= MAX_SIDE {
        return Ok((Cow::Borrowed(bytes), mime));
    }
    let decoded = image::load_from_memory(bytes)?;
    let decoded = if width.max(height) > MAX_SIDE {
        decoded.resize(MAX_SIDE, MAX_SIDE, image::imageops::FilterType::Lanczos3)
    } else {
        decoded
    };
    let mut background = image::RgbaImage::from_pixel(
        decoded.width(),
        decoded.height(),
        image::Rgba([255, 255, 255, 255]),
    );
    image::imageops::overlay(&mut background, &decoded.to_rgba8(), 0, 0);
    let mut image = image::DynamicImage::ImageRgba8(background).into_rgb8();
    loop {
        let mut encoded = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut encoded, 92)
            .encode_image(&image)?;
        if encoded.len() <= MAX_BYTES {
            return Ok((Cow::Owned(encoded), "image/jpeg"));
        }
        let width = (image.width() * 3 / 4).max(1);
        let height = (image.height() * 3 / 4).max(1);
        image =
            image::imageops::resize(&image, width, height, image::imageops::FilterType::Lanczos3);
    }
}

impl ModelClient {
    pub fn new(config: &Config) -> Result<Self> {
        config.validate()?;
        let identity = format!(
            "{}:{}:{}",
            config.provider.base_url, config.provider.namespace, config.provider.key_env
        );
        let rate = RATE_POOLS
            .get_or_init(|| std::sync::Mutex::new(HashMap::new()))
            .lock()
            .map_err(|_| anyhow::anyhow!("Provider rate limiter unavailable"))?
            .entry(identity)
            .or_insert_with(|| {
                Arc::new(Mutex::new(Rate {
                    next: Instant::now(),
                    tokens: VecDeque::new(),
                }))
            })
            .clone();
        let http = {
            let mut pools = HTTP_POOLS
                .get_or_init(|| std::sync::Mutex::new(HashMap::new()))
                .lock()
                .map_err(|_| anyhow::anyhow!("HTTP connection pool unavailable"))?;
            if let Some(client) = pools.get(&config.provider.timeout_seconds) {
                client.clone()
            } else {
                let client = reqwest::Client::builder()
                    .timeout(Duration::from_secs(config.provider.timeout_seconds))
                    .connect_timeout(Duration::from_secs(20))
                    .redirect(reqwest::redirect::Policy::none())
                    .build()?;
                pools.insert(config.provider.timeout_seconds, client.clone());
                client
            }
        };
        Ok(Self {
            http,
            config: config.clone(),
            rate,
            pending: Arc::new(Mutex::new(HashMap::new())),
            slots: Arc::new(tokio::sync::Semaphore::new(config.provider.concurrency)),
        })
    }
    pub fn configured(&self, vision: bool) -> bool {
        let p = &self.config.provider;
        let model = if vision && !p.vision_model.is_empty() {
            &p.vision_model
        } else {
            &p.model
        };
        !model.trim().is_empty()
    }
    fn reserve(
        &self,
        library: &Library,
        job: i64,
        key: &str,
        detail: &str,
        cost: Option<f64>,
    ) -> Result<i64> {
        // Own the write lock before reading counters: concurrent requests cannot
        // reuse the same allowance. Unsettled reservations survive worker crashes.
        let transaction = rusqlite::Transaction::new_unchecked(
            &library.db,
            rusqlite::TransactionBehavior::Immediate,
        )?;
        let state = library.job(job)?;
        if state.status != "running" {
            bail!("Job paused or cancelled");
        }
        let (tokens,reserved_cost):(i64,f64)=library.db.query_row("SELECT COALESCE(SUM(output_tokens),0),COALESCE(SUM(cost),0) FROM request_reservations WHERE job=? AND settled=0",[job],|r|Ok((r.get(0)?,r.get(1)?)))?;
        let p = &self.config.provider;
        let limits = &self.config.processing;
        if state.requests as u64 >= limits.max_requests
            || state.output_tokens as u64 + tokens as u64 + p.max_output_tokens as u64
                > limits.max_generated_tokens
        {
            bail!("budget_exhausted: request or output-token ceiling reached");
        }
        if limits
            .max_cost
            .zip(cost)
            .is_some_and(|(cap, next)| state.cost.unwrap_or(0.0) + reserved_cost + next > cap)
        {
            bail!("budget_exhausted: estimated next request exceeds cost ceiling");
        }
        library.db.execute(
            "UPDATE jobs SET requests=requests+1,updated=? WHERE id=?",
            rusqlite::params![now(), job],
        )?;
        library.db.execute(
            "INSERT INTO attempts(job,cache_key,status,detail,created) VALUES(?,?,'started',?,?)",
            rusqlite::params![job, key, detail, now()],
        )?;
        let id = library.db.last_insert_rowid();
        library.db.execute(
            "INSERT INTO request_reservations(attempt,job,output_tokens,cost) VALUES(?,?,?,?)",
            rusqlite::params![id, job, i64::try_from(p.max_output_tokens)?, cost],
        )?;
        transaction.commit()?;
        Ok(id)
    }
    fn payload(&self, request: &Request<'_>) -> Result<(Value, String)> {
        let p = &self.config.provider;
        let image = request
            .image
            .map(|(bytes, mime)| transport_image(bytes, mime))
            .transpose()?;
        let model = if request.image.is_some() && !p.vision_model.is_empty() {
            &p.vision_model
        } else {
            &p.model
        };
        if model.trim().is_empty() {
            bail!("No model configured. Use librarian setup or config set provider.model MODEL");
        }
        let plain = request.schema.is_null();
        let input = if plain {
            request
                .input
                .as_str()
                .map(str::to_owned)
                .unwrap_or(serde_json::to_string(&request.input)?)
        } else {
            serde_json::to_string(&request.input)?
        };
        let instructions = if plain || p.structured_output == "schema" {
            request.instructions.to_owned()
        } else {
            format!(
                "{}\nReturn only JSON conforming to this schema:\n{}",
                request.instructions, request.schema
            )
        };
        let mut body = match p.kind.as_str() {
            "responses" => {
                let mut prefix = json!({"type":"input_text","text":instructions});
                if p.prompt_cache == "explicit" {
                    prefix["prompt_cache_breakpoint"] = json!({"mode":"explicit","ttl":"30m"});
                }
                let mut content = vec![json!({"type":"input_text","text":input})];
                if let Some((bytes, mime)) = &image {
                    content.push(json!({"type":"input_image","image_url":format!("data:{mime};base64,{}",base64::engine::general_purpose::STANDARD.encode(bytes)),"detail":"high"}));
                }
                let mut body = json!({"model":model,"store":false,"input":[{"role":"developer","content":[prefix]},{"role":"user","content":content}],"max_output_tokens":p.max_output_tokens});
                if let Some(context) = request.context {
                    body["input"][0]["content"]
                        .as_array_mut()
                        .unwrap()
                        .push(json!({"type":"input_text","text":context}));
                }
                if !plain && p.structured_output == "schema" {
                    body["text"] = json!({"format":{"type":"json_schema","name":request.task,"strict":true,"schema":request.schema}});
                } else if !plain && p.structured_output == "json" {
                    body["text"] = json!({"format":{"type":"json_object"}});
                }
                body
            }
            "anthropic" => {
                let mut prefix = json!({"type":"text","text":instructions});
                if p.prompt_cache == "ephemeral" {
                    prefix["cache_control"] = json!({"type":"ephemeral"});
                }
                let mut content = vec![json!({"type":"text","text":input})];
                if let Some((bytes, mime)) = &image {
                    content.push(json!({"type":"image","source":{"type":"base64","media_type":mime,"data":base64::engine::general_purpose::STANDARD.encode(bytes)}}));
                }
                let mut body = json!({"model":model,"max_tokens":p.max_output_tokens,"system":[prefix],"messages":[{"role":"user","content":content}]});
                if let Some(context) = request.context {
                    body["system"]
                        .as_array_mut()
                        .unwrap()
                        .push(json!({"type":"text","text":context}));
                }
                if !plain && p.structured_output == "schema" {
                    body["output_config"] =
                        json!({"format":{"type":"json_schema","schema":request.schema}});
                }
                body
            }
            _ => {
                let mut content = vec![json!({"type":"text","text":input})];
                if let Some((bytes, mime)) = &image {
                    content.push(json!({"type":"image_url","image_url":{"url":format!("data:{mime};base64,{}",base64::engine::general_purpose::STANDARD.encode(bytes))}}));
                }
                let mut body = json!({"model":model,"messages":[{"role":"system","content":instructions},{"role":"user","content":content}],"max_tokens":p.max_output_tokens});
                if let Some(context) = request.context {
                    body["messages"]
                        .as_array_mut()
                        .unwrap()
                        .insert(1, json!({"role":"system","content":context}));
                }
                if !plain && p.structured_output == "schema" {
                    body["response_format"] = json!({"type":"json_schema","json_schema":{"name":request.task,"strict":true,"schema":request.schema}});
                } else if !plain && p.structured_output == "json" {
                    body["response_format"] = json!({"type":"json_object"});
                }
                body
            }
        };
        if p.kind == "responses" && p.prompt_cache != "off" {
            body["prompt_cache_key"] = json!(format!("librarian:{}:{}", p.namespace, request.task));
            if p.prompt_cache == "explicit" {
                body["prompt_cache_options"] = json!({"mode":"explicit","ttl":"30m"});
            }
        }
        let suffix = match p.kind.as_str() {
            "responses" => "responses",
            "anthropic" => "messages",
            _ => "chat/completions",
        };
        Ok((
            body,
            format!("{}/{suffix}", p.base_url.trim_end_matches('/')),
        ))
    }
    async fn throttle(&self, estimated: u64, library: &Library, job: i64) -> Result<()> {
        let p = &self.config.provider;
        if estimated > p.tokens_per_minute {
            bail!(
                "A request exceeds the configured tokens-per-minute allowance; reduce chunk size or increase the allowance"
            );
        }
        loop {
            if !library.active(job)? {
                bail!("Job paused or cancelled");
            }
            let mut rate = self.rate.lock().await;
            let now = Instant::now();
            while rate
                .tokens
                .front()
                .is_some_and(|(at, _)| now.duration_since(*at) >= Duration::from_secs(60))
            {
                rate.tokens.pop_front();
            }
            let wait_rate = rate.next.saturating_duration_since(now);
            let used: u64 = rate.tokens.iter().map(|(_, n)| n).sum();
            let wait_tokens = if used + estimated > p.tokens_per_minute {
                rate.tokens
                    .front()
                    .map(|(at, _)| Duration::from_secs(60).saturating_sub(now.duration_since(*at)))
                    .unwrap_or_default()
            } else {
                Duration::ZERO
            };
            let wait = wait_rate.max(wait_tokens);
            if wait.is_zero() {
                rate.tokens.push_back((now, estimated));
                rate.next = now + Duration::from_secs_f64(60.0 / f64::from(p.requests_per_minute));
                return Ok(());
            }
            drop(rate);
            tokio::time::sleep(wait.min(Duration::from_secs(1))).await;
        }
    }
    pub async fn request(
        &self,
        library: &Library,
        job: i64,
        request: Request<'_>,
    ) -> Result<Value> {
        let (body, url) = self.payload(&request)?;
        let p = &self.config.provider;
        let separate_vision =
            request.image.is_some() && !p.vision_model.is_empty() && p.vision_model != p.model;
        let (input_price, cached_price, output_price) = if separate_vision {
            (
                p.vision_input_price_per_million,
                p.vision_cached_input_price_per_million,
                p.vision_output_price_per_million,
            )
        } else {
            (
                p.input_price_per_million,
                p.cached_input_price_per_million,
                p.output_price_per_million,
            )
        };
        let key = blake3::hash(&serde_json::to_vec(&(
            "inference-v2",
            &url,
            &p.namespace,
            &body,
        ))?)
        .to_hex()
        .to_string();
        let pending = {
            let mut requests = self.pending.lock().await;
            requests.retain(|_, request| request.strong_count() > 0);
            let lock = requests
                .get(&key)
                .and_then(Weak::upgrade)
                .unwrap_or_else(|| Arc::new(Mutex::new(())));
            requests.insert(key.clone(), Arc::downgrade(&lock));
            lock
        };
        let _same_request = pending.lock().await;
        if let Some(value) = library.cache(&key, p.cache_ttl_hours)? {
            if validate_schema(&value, &request.schema, "$", 0)
                .and_then(|()| (request.validate)(&value))
                .is_ok()
            {
                library.db.execute(
                    "UPDATE jobs SET cache_hits=cache_hits+1,updated=? WHERE id=?",
                    rusqlite::params![now(), job],
                )?;
                return Ok(value);
            }
            library
                .db
                .execute("DELETE FROM inference_cache WHERE key=?", [&key])?;
            library.event(
                job,
                "warning",
                "Discarded a cached response that failed the current content contract",
            )?;
        }
        let _slot = self.slots.acquire().await?;
        // UTF-8 bytes conservatively bound byte-BPE text tokenization. Image allowance
        // is separate and deliberately explicit; providers return actual billed usage.
        let input_estimate = (request.instructions.len()
            + request.context.map_or(0, str::len)
            + serde_json::to_vec(&request.input)?.len()
            + serde_json::to_vec(&request.schema)?.len()
            + 1024) as u64
            + if request.image.is_some() { 8192 } else { 0 };
        if input_estimate + p.max_output_tokens as u64 > p.context_tokens as u64 {
            bail!("Request exceeds the configured context budget");
        }
        let estimated = input_estimate + p.max_output_tokens as u64;
        let provider = p.clone();
        let key_value =
            tokio::task::spawn_blocking(move || crate::credentials::resolve(&provider)).await??;
        let local = reqwest::Url::parse(&p.base_url)?
            .host_str()
            .is_some_and(|h| matches!(h, "localhost" | "127.0.0.1" | "::1" | "[::1]"));
        if key_value.is_none() && !local {
            bail!(
                "API credential is missing. Open Setup to connect your provider, or configure {} for scripted use.",
                p.key_env
            );
        }
        for attempt in 0..p.attempts {
            self.throttle(estimated, library, job).await?;
            if !library.active(job)? {
                bail!("Job paused or cancelled");
            }
            let reserve_cost = match (input_price, output_price) {
                (Some(i), Some(o)) => Some(
                    (input_estimate as f64 * i * 2.5 + p.max_output_tokens as f64 * o)
                        / 1_000_000.0,
                ),
                _ => None,
            };
            let attempt_id = self.reserve(
                library,
                job,
                &key,
                &format!("{} attempt {}", request.task, attempt + 1),
                reserve_cost,
            )?;
            let mut req = self.http.post(&url).json(&body);
            if let Some(value) = &key_value {
                req = if p.kind == "anthropic" {
                    req.header("x-api-key", value.as_str())
                } else {
                    req.bearer_auth(value.as_str())
                };
            }
            if p.kind == "anthropic" {
                req = req.header("anthropic-version", "2023-06-01");
            }
            let sending = req.send();
            tokio::pin!(sending);
            let response = loop {
                tokio::select! {
                    response=&mut sending => break response,
                    _=tokio::time::sleep(Duration::from_millis(500)) => {
                        if !library.active(job)? {bail!("Job paused or cancelled; in-flight billing remains reserved");}
                    }
                }
            };
            let mut retry_after = None;
            let value = match response {
                Ok(mut response) => {
                    let status = response.status();
                    retry_after = response
                        .headers()
                        .get("retry-after")
                        .and_then(|h| h.to_str().ok())
                        .and_then(|s| s.parse::<u64>().ok());
                    if response
                        .content_length()
                        .is_some_and(|n| n > 16 * 1024 * 1024)
                    {
                        bail!("Provider response exceeds 16 MiB limit");
                    }
                    if status.is_success() {
                        let mut bytes = Vec::new();
                        while let Some(chunk) = response.chunk().await? {
                            if bytes.len() + chunk.len() > 16 * 1024 * 1024 {
                                bail!("Provider response exceeds 16 MiB limit");
                            }
                            bytes.extend_from_slice(&chunk);
                        }
                        Some(
                            serde_json::from_slice::<Value>(&bytes)
                                .context("Provider returned invalid JSON")?,
                        )
                    } else if status.as_u16() == 429 || status.is_server_error() {
                        if status.as_u16() == 429 {
                            library.db.execute(
                                "UPDATE request_reservations SET settled=1 WHERE attempt=?",
                                [attempt_id],
                            )?;
                        }
                        library.event(
                            job,
                            "warning",
                            &format!(
                                "Provider HTTP {}; retry {}/{}",
                                status.as_u16(),
                                attempt + 1,
                                p.attempts
                            ),
                        )?;
                        None
                    } else {
                        library.db.execute(
                            "UPDATE request_reservations SET settled=1 WHERE attempt=?",
                            [attempt_id],
                        )?;
                        library.db.execute(
                            "UPDATE attempts SET status='failed',detail=? WHERE id=?",
                            rusqlite::params![format!("HTTP {}", status.as_u16()), attempt_id],
                        )?;
                        bail!(
                            "Provider HTTP {}. Check credentials, model access and endpoint configuration.",
                            status.as_u16()
                        );
                    }
                }
                Err(error) => {
                    library.event(
                        job,
                        "warning",
                        if error.is_timeout() {
                            "Provider timed out; billing outcome may be unknown"
                        } else {
                            "Provider transport failed"
                        },
                    )?;
                    None
                }
            };
            if let Some(value) = value {
                let usage = &value["usage"];
                let reported_input = usage["input_tokens"]
                    .as_i64()
                    .or_else(|| usage["prompt_tokens"].as_i64());
                let reported_output = usage["output_tokens"]
                    .as_i64()
                    .or_else(|| usage["completion_tokens"].as_i64());
                let input = reported_input.unwrap_or(0).max(0);
                let output = reported_output.unwrap_or(0).max(0);
                let cached = usage["input_tokens_details"]["cached_tokens"]
                    .as_i64()
                    .or_else(|| usage["prompt_tokens_details"]["cached_tokens"].as_i64())
                    .or_else(|| usage["cache_read_input_tokens"].as_i64())
                    .unwrap_or(0)
                    .max(0);
                let writes = usage["input_tokens_details"]["cache_write_tokens"]
                    .as_i64()
                    .or_else(|| usage["cache_creation_input_tokens"].as_i64())
                    .unwrap_or(0)
                    .max(0);
                // Anthropic reports uncached input separately; OpenAI includes cache
                // reads and writes in its input total.
                let total_input = if p.kind == "anthropic" {
                    input + cached + writes
                } else {
                    input
                };
                let uncached = (total_input - cached - writes).max(0);
                let cost = match (input_price, output_price, reported_input, reported_output) {
                    (Some(i), Some(o), Some(_), Some(_)) => Some(
                        (uncached as f64 * i
                            + cached as f64 * cached_price.unwrap_or(i)
                            + writes as f64 * i * 1.25
                            + output as f64 * o)
                            / 1_000_000.0,
                    ),
                    _ => None,
                };
                let accounting = library.db.unchecked_transaction()?;
                library.db.execute("UPDATE jobs SET input_tokens=input_tokens+?,output_tokens=output_tokens+?,cached_tokens=cached_tokens+?,cost=CASE WHEN ? IS NULL THEN cost ELSE COALESCE(cost,0)+? END,updated=? WHERE id=?",rusqlite::params![total_input,output,cached,cost,cost,now(),job])?;
                if reported_input.is_some() && reported_output.is_some() {
                    library.db.execute(
                        "UPDATE request_reservations SET settled=1 WHERE attempt=?",
                        [attempt_id],
                    )?;
                }
                library.db.execute(
                    "UPDATE attempts SET status='received',detail=? WHERE id=?",
                    rusqlite::params![
                        serde_json::to_string(
                            &json!({"response_id":value["id"],"model":value["model"],"usage":usage})
                        )?,
                        attempt_id
                    ],
                )?;
                accounting.commit()?;
                let result = if request.schema.is_null() {
                    Value::String(decode_text(&value, &p.kind)?)
                } else {
                    decode(&value, &p.kind)?
                };
                validate_schema(&result, &request.schema, "$", 0)?;
                if let Err(error) = (request.validate)(&result) {
                    library.db.execute(
                        "UPDATE attempts SET status='rejected',detail=? WHERE id=?",
                        rusqlite::params![error.to_string(), attempt_id],
                    )?;
                    return Err(error);
                }
                // Cache computation independently of publication; cancelled work cannot
                // revive the job, but the exact successful response may be reused.
                library.cache_put(&key, &result)?;
                return Ok(result);
            }
            library.db.execute(
                "UPDATE attempts SET status='retryable' WHERE id=?",
                [attempt_id],
            )?;
            if attempt + 1 < p.attempts {
                let jitter = (now().unsigned_abs() + attempt as u64 * 17) % 3;
                let seconds = retry_after.unwrap_or((2u64.pow(attempt + 1)) + jitter);
                if seconds > self.config.provider.timeout_seconds {
                    bail!("Provider retry delay exceeds configured deadline; resume later");
                }
                for _ in 0..seconds {
                    if !library.active(job)? {
                        bail!("Job paused or cancelled");
                    }
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }
        }
        bail!("Provider retries exhausted; completed work is retained for resume")
    }
}

fn decode(value: &Value, kind: &str) -> Result<Value> {
    serde_json::from_str(&decode_text(value, kind)?)
        .context("Provider output does not match the required JSON contract")
}

#[derive(Debug, thiserror::Error)]
#[error("Provider output limit reached; subdivide the source and continue")]
pub struct OutputLimit;

fn decode_text(value: &Value, kind: &str) -> Result<String> {
    if value["incomplete_details"]["reason"] == "max_output_tokens"
        || value["stop_reason"] == "max_tokens"
        || value["choices"][0]["finish_reason"] == "length"
    {
        return Err(OutputLimit.into());
    }
    let text = match kind {
        "responses" => {
            if value["status"].as_str() != Some("completed") {
                bail!("Provider response is incomplete; source retained");
            }
            value["output"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|item| item["content"].as_array())
                .flatten()
                .filter(|c| c["type"] == "output_text")
                .filter_map(|c| c["text"].as_str())
                .collect::<String>()
        }
        "anthropic" => {
            if value["stop_reason"].as_str() != Some("end_turn") {
                bail!("Provider response did not complete normally");
            }
            value["content"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|c| c["type"] == "text")
                .filter_map(|c| c["text"].as_str())
                .collect::<String>()
        }
        _ => {
            if value["choices"][0]["finish_reason"].as_str() != Some("stop") {
                bail!("Provider response truncated or refused");
            }
            value["choices"][0]["message"]["content"]
                .as_str()
                .unwrap_or("")
                .to_owned()
        }
    };
    Ok(text)
}

pub fn object_schema(properties: Value, required: &[&str]) -> Value {
    json!({"type":"object","properties":properties,"required":required,"additionalProperties":false})
}

// Validate the deliberately small JSON Schema vocabulary emitted by Librarian.
// Provider-side structured output is an optimization, never the trust boundary.
fn validate_schema(value: &Value, schema: &Value, path: &str, depth: usize) -> Result<()> {
    if depth > 64 {
        bail!("Provider JSON nesting exceeds limit at {path}");
    }
    if let Some(alternatives) = schema
        .get("oneOf")
        .or_else(|| schema.get("anyOf"))
        .and_then(Value::as_array)
    {
        let matches = alternatives
            .iter()
            .filter(|s| validate_schema(value, s, path, depth + 1).is_ok())
            .count();
        if matches == 0 || (schema.get("oneOf").is_some() && matches != 1) {
            bail!("Provider JSON has invalid variant at {path}");
        }
        return Ok(());
    }
    if schema
        .get("const")
        .is_some_and(|expected| expected != value)
        || schema
            .get("enum")
            .and_then(Value::as_array)
            .is_some_and(|options| !options.contains(value))
    {
        bail!("Provider JSON has invalid value at {path}");
    }
    let matches_type = |kind: &str| match kind {
        "object" => value.is_object(),
        "array" => value.is_array(),
        "string" => value.is_string(),
        "integer" => value.is_i64() || value.is_u64(),
        "number" => value.is_number(),
        "boolean" => value.is_boolean(),
        "null" => value.is_null(),
        _ => false,
    };
    if let Some(kind) = schema.get("type") {
        let valid = kind.as_str().is_some_and(matches_type)
            || kind
                .as_array()
                .is_some_and(|kinds| kinds.iter().filter_map(Value::as_str).any(matches_type));
        if !valid {
            bail!("Provider JSON has wrong type at {path}");
        }
    }
    if let Some(object) = value.as_object() {
        for key in schema["required"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            if !object.contains_key(key) {
                bail!("Provider JSON missing {path}.{key}");
            }
        }
        for (key, item) in object {
            if let Some(property) = schema["properties"].get(key) {
                validate_schema(item, property, &format!("{path}.{key}"), depth + 1)?;
            } else if schema["additionalProperties"] == false {
                bail!("Provider JSON unexpected field {path}.{key}");
            }
        }
    }
    if let Some(items) = value.as_array() {
        if schema["maxItems"]
            .as_u64()
            .is_some_and(|n| items.len() as u64 > n)
            || schema["minItems"]
                .as_u64()
                .is_some_and(|n| (items.len() as u64) < n)
        {
            bail!("Provider JSON array length invalid at {path}");
        }
        if let Some(item_schema) = schema.get("items") {
            for (i, item) in items.iter().enumerate() {
                validate_schema(item, item_schema, &format!("{path}[{i}]"), depth + 1)?;
            }
        }
    }
    if let Some(number) = value.as_f64()
        && (schema["minimum"].as_f64().is_some_and(|n| number < n)
            || schema["maximum"].as_f64().is_some_and(|n| number > n))
    {
        bail!("Provider JSON number outside limits at {path}");
    }
    Ok(())
}

#[cfg(test)]
mod budget_invariants {
    use super::*;

    // An actual high-entropy PNG crosses the transport limit. Exercise the
    // provider envelope, base64 and real codec, not a mocked request/response.
    #[test]
    fn large_scan_payload_is_bounded_and_decodable() {
        let mut random = 0x12345678u32;
        let mut pixels = image::RgbImage::new(1100, 1000);
        for byte in pixels.as_mut() {
            random ^= random << 13;
            random ^= random >> 17;
            random ^= random << 5;
            *byte = (random >> 8) as u8;
        }
        let mut png = std::io::Cursor::new(Vec::new());
        pixels.write_to(&mut png, image::ImageFormat::Png).unwrap();
        assert!(png.get_ref().len() > 3 * 1024 * 1024);
        let mut config = Config::default();
        config.provider.kind = "anthropic".into();
        config.provider.model = "transport-invariant".into();
        let client = ModelClient::new(&config).unwrap();
        let (body, _) = client
            .payload(&Request {
                task: "vision",
                instructions: "Read the source image.",
                context: None,
                input: Value::String("Page evidence".into()),
                schema: Value::Null,
                image: Some((png.get_ref(), "image/png")),
                validate: &|_| Ok(()),
            })
            .unwrap();
        let source = &body["messages"][0]["content"][1]["source"];
        let data = source["data"].as_str().unwrap();
        assert!(data.len() <= 4 * 1024 * 1024);
        let decoded = image::load_from_memory(
            &base64::engine::general_purpose::STANDARD
                .decode(data)
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            (decoded.width(), decoded.height()),
            (1100, 1000),
            "JPEG should fit without losing scan resolution"
        );
        assert_eq!(source["media_type"], "image/jpeg");
        // A compact text-page PNG keeps its exact lossless representation.
        let mut png = std::io::Cursor::new(Vec::new());
        image::RgbImage::from_pixel(16, 16, image::Rgb([255, 255, 255]))
            .write_to(&mut png, image::ImageFormat::Png)
            .unwrap();
        let (bytes, mime) = transport_image(png.get_ref(), "image/png").unwrap();
        assert_eq!(bytes.as_ref(), png.get_ref());
        assert_eq!(mime, "image/png");
    }

    // Real concurrent SQLite connections exercise the spending transaction. No
    // provider or transport is mocked; this checks the boundary before dispatch.
    #[test]
    fn concurrent_reservations_and_restart_do_not_reuse_budget() {
        for cost_ceiling in [true, false] {
            let temp = tempfile::tempdir().unwrap();
            let mut config = Config::default();
            config.provider.max_output_tokens = 256;
            config.provider.input_price_per_million = Some(1.0);
            config.provider.output_price_per_million = Some(1.0);
            config.processing.max_generated_tokens = if cost_ceiling { 1_000_000 } else { 1024 };
            config.processing.max_cost = cost_ceiling.then_some(0.5);
            let library = Library::open(temp.path()).unwrap();
            let job = library
                .enqueue(&temp.path().join("source.txt"), temp.path(), &config)
                .unwrap();
            library.start(job).unwrap();
            let client = ModelClient::new(&config).unwrap();
            let barrier = Arc::new(std::sync::Barrier::new(8));
            let connections = (0..8)
                .map(|_| Library::open(temp.path()).unwrap())
                .collect::<Vec<_>>();
            let workers = connections
                .into_iter()
                .enumerate()
                .map(|(i, library)| {
                    let barrier = barrier.clone();
                    let client = client.clone();
                    std::thread::spawn(move || {
                        barrier.wait();
                        client.reserve(
                            &library,
                            job,
                            &format!("request-{i}"),
                            "parallel request",
                            Some(0.125),
                        )
                    })
                })
                .collect::<Vec<_>>();
            let results = workers
                .into_iter()
                .map(|w| w.join().unwrap())
                .collect::<Vec<_>>();
            assert_eq!(
                results.iter().filter(|r| r.is_ok()).count(),
                4,
                "Exactly four reservations fit this independent ceiling"
            );
            for error in results.into_iter().filter_map(Result::err) {
                assert!(
                    error.to_string().starts_with("budget_exhausted:"),
                    "Unexpected reservation error: {error}"
                );
            }
            assert_eq!(library.job(job).unwrap().requests, 4);
            drop(library);
            let reopened = Library::open(temp.path()).unwrap();
            let fresh = ModelClient::new(&config).unwrap();
            assert!(
                fresh
                    .reserve(
                        &reopened,
                        job,
                        "after-restart",
                        "unknown billing must remain reserved",
                        Some(0.125)
                    )
                    .unwrap_err()
                    .to_string()
                    .starts_with("budget_exhausted:")
            );
            assert_eq!(reopened.job(job).unwrap().requests, 4);
        }
    }
}
