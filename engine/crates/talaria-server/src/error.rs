//! Structured `{ "code": ..., "message": ... }` HTTP error envelope and Axum extractors.

use std::fmt;

use axum::extract::rejection::{JsonRejection, PathRejection, QueryRejection};
use axum::extract::{FromRequest, FromRequestParts, Path, Query, Request};
use axum::http::StatusCode;
use axum::http::request::Parts;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::de::{self, DeserializeOwned, Visitor};
use serde::{Deserializer, Serialize};
use talaria_contracts::{ErrorResponse, error_codes};
use talaria_feed::FeedError;
use talaria_operators::OperatorError;
use talaria_packs::PackError;
use talaria_store::StoreError;

pub const SUBSCRIPTION_QUOTA: &str = "SUBSCRIPTION_QUOTA";

/// Unified API error mapped strictly to `{ "code": ..., "message": ... }` JSON responses.
#[derive(Debug, Clone)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ApiError {}

impl ApiError {
    pub fn new(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status,
            code,
            message: message.into(),
        }
    }

    pub fn with_status(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
        Self::new(status, code, message)
    }

    pub fn service_unavailable(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, code, message)
    }

    pub fn unauthorized(message: impl Into<String>) -> Self {
        Self::new(StatusCode::UNAUTHORIZED, error_codes::UNAUTHORIZED, message)
    }

    pub fn not_found(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, code, message)
    }

    pub fn validation(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            error_codes::VALIDATION_ERROR,
            message,
        )
    }

    pub fn validation_code(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, code, message)
    }

    pub fn range_too_large(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::BAD_REQUEST,
            error_codes::BARS_RANGE_TOO_LARGE,
            message,
        )
    }

    pub fn conflict(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(StatusCode::CONFLICT, code, message)
    }

    pub fn not_implemented(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::NOT_IMPLEMENTED,
            error_codes::NOT_IMPLEMENTED,
            message,
        )
    }

    pub fn feed_unavailable(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::SERVICE_UNAVAILABLE,
            error_codes::FEED_UNAVAILABLE,
            message,
        )
    }

    pub fn timeout(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::GATEWAY_TIMEOUT,
            error_codes::BARS_BACKFILL_TIMEOUT,
            message,
        )
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            error_codes::INTERNAL,
            message,
        )
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = ErrorResponse {
            code: self.code.to_owned(),
            message: self.message,
        };
        (self.status, Json(body)).into_response()
    }
}

impl From<talaria_contracts::ContractError> for ApiError {
    fn from(err: talaria_contracts::ContractError) -> Self {
        Self::validation(err.to_string())
    }
}

impl From<OperatorError> for ApiError {
    fn from(err: OperatorError) -> Self {
        Self::validation(err.to_string())
    }
}

impl From<PackError> for ApiError {
    fn from(err: PackError) -> Self {
        match err {
            PackError::InstrumentNotFound(msg) => {
                Self::not_found(error_codes::INSTRUMENT_NOT_FOUND, msg)
            }
            PackError::InvalidRequest(msg) => Self::validation(msg),
            PackError::FeedUnavailable(msg) | PackError::FeedTransport(msg) => {
                Self::feed_unavailable(msg)
            }
            PackError::Contract(inner) => inner.into(),
            other => Self::feed_unavailable(other.to_string()),
        }
    }
}

impl From<FeedError> for ApiError {
    fn from(err: FeedError) -> Self {
        match err {
            FeedError::InstrumentNotFound(msg) => {
                Self::not_found(error_codes::INSTRUMENT_NOT_FOUND, msg)
            }
            FeedError::InvalidRequest(msg) | FeedError::ProtectedDataDir(msg) => {
                Self::validation(msg)
            }
            FeedError::Pack(inner) => inner.into(),
            FeedError::Contract(inner) => inner.into(),
            FeedError::Transport(msg) => Self::feed_unavailable(msg),
            FeedError::Io(err) => Self::internal(err.to_string()),
        }
    }
}

impl From<StoreError> for ApiError {
    fn from(err: StoreError) -> Self {
        match err {
            StoreError::ServiceLocked(msg) => Self::conflict(error_codes::SERVICE_LOCKED, msg),
            StoreError::SingleWriterViolation(msg) => {
                Self::conflict(error_codes::SINGLE_WRITER_VIOLATION, msg)
            }
            StoreError::Quality(msg) => Self::validation_code(error_codes::COLD_STORE_QUALITY, msg),
            StoreError::Integrity(msg) => Self::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                error_codes::COLD_STORE_INTEGRITY,
                msg,
            ),
            StoreError::Contract(inner) => inner.into(),
            other => Self::internal(other.to_string()),
        }
    }
}

/// JSON body extractor and responder returning `422 { "code": "VALIDATION_ERROR", "message": ... }` on failure.
pub struct ApiJson<T>(pub T);

impl<T: Serialize> IntoResponse for ApiJson<T> {
    fn into_response(self) -> Response {
        Json(self.0).into_response()
    }
}

impl<S, T> FromRequest<S> for ApiJson<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        match Json::<T>::from_request(req, state).await {
            Ok(Json(value)) => Ok(Self(value)),
            Err(err) => Err(json_rejection_to_api(err)),
        }
    }
}

fn json_rejection_to_api(err: JsonRejection) -> ApiError {
    ApiError::validation(err.body_text())
}

/// Query extractor returning `422 { "code": "VALIDATION_ERROR", "message": ... }` on failure.
pub struct ApiQuery<T>(pub T);

impl<S, T> FromRequestParts<S> for ApiQuery<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        match Query::<T>::from_request_parts(parts, state).await {
            Ok(Query(value)) => Ok(Self(value)),
            Err(err) => Err(query_rejection_to_api(err)),
        }
    }
}

fn query_rejection_to_api(err: QueryRejection) -> ApiError {
    ApiError::validation(err.body_text())
}

/// Path extractor returning `422 { "code": "VALIDATION_ERROR", "message": ... }` on failure.
pub struct ApiPath<T>(pub T);

impl<S, T> FromRequestParts<S> for ApiPath<T>
where
    T: DeserializeOwned + Send,
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        match Path::<T>::from_request_parts(parts, state).await {
            Ok(Path(value)) => Ok(Self(value)),
            Err(err) => Err(path_rejection_to_api(err)),
        }
    }
}

fn path_rejection_to_api(err: PathRejection) -> ApiError {
    ApiError::validation(err.body_text())
}

fn parse_lenient_bool_str(raw: &str) -> Result<bool, String> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" | "t" | "y" => Ok(true),
        "false" | "0" | "no" | "off" | "f" | "n" => Ok(false),
        _ => Err(format!("invalid boolean query value: {raw:?}")),
    }
}

/// Deserialize a query/header boolean accepting `"1"`/`"0"`/`"true"`/`"false"`/`"yes"`/`"no"`/`"on"`/`"off"`.
pub fn deserialize_lenient_bool<'de, D>(deserializer: D) -> Result<bool, D::Error>
where
    D: Deserializer<'de>,
{
    struct BoolVisitor;

    impl Visitor<'_> for BoolVisitor {
        type Value = bool;

        fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
            formatter.write_str("a boolean or boolean string such as true/false/1/0")
        }

        fn visit_bool<E: de::Error>(self, v: bool) -> Result<bool, E> {
            Ok(v)
        }

        fn visit_i64<E: de::Error>(self, v: i64) -> Result<bool, E> {
            match v {
                1 => Ok(true),
                0 => Ok(false),
                _ => Err(E::custom(format!("invalid integer boolean: {v}"))),
            }
        }

        fn visit_u64<E: de::Error>(self, v: u64) -> Result<bool, E> {
            match v {
                1 => Ok(true),
                0 => Ok(false),
                _ => Err(E::custom(format!("invalid integer boolean: {v}"))),
            }
        }

        fn visit_str<E: de::Error>(self, v: &str) -> Result<bool, E> {
            parse_lenient_bool_str(v).map_err(E::custom)
        }
    }

    deserializer.deserialize_any(BoolVisitor)
}

/// Deserialize an optional query boolean accepting `"1"`/`"0"`/`"true"`/`"false"` or empty string.
pub fn deserialize_lenient_opt_bool<'de, D>(deserializer: D) -> Result<Option<bool>, D::Error>
where
    D: Deserializer<'de>,
{
    struct OptBoolVisitor;

    impl<'de> Visitor<'de> for OptBoolVisitor {
        type Value = Option<bool>;

        fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
            formatter.write_str("an optional boolean or boolean string")
        }

        fn visit_none<E: de::Error>(self) -> Result<Option<bool>, E> {
            Ok(None)
        }

        fn visit_unit<E: de::Error>(self) -> Result<Option<bool>, E> {
            Ok(None)
        }

        fn visit_some<D2: Deserializer<'de>>(self, deserializer: D2) -> Result<Option<bool>, D2::Error> {
            deserialize_lenient_bool(deserializer).map(Some)
        }

        fn visit_bool<E: de::Error>(self, v: bool) -> Result<Option<bool>, E> {
            Ok(Some(v))
        }

        fn visit_str<E: de::Error>(self, v: &str) -> Result<Option<bool>, E> {
            if v.trim().is_empty() {
                Ok(None)
            } else {
                parse_lenient_bool_str(v).map(Some).map_err(E::custom)
            }
        }
    }

    deserializer.deserialize_any(OptBoolVisitor)
}
