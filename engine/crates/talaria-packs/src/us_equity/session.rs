//! US Equity trading sessions, `America/New_York` DST arithmetic, and NYSE/NASDAQ holidays.

use serde::{Deserialize, Serialize};
use talaria_contracts::{SessionTrait, TradingSession};

use crate::us_equity::AlpacaDataFeed;

const SEC_MS: i64 = 1_000;
const DAY_SECS: i64 = 86_400;
const EST_OFFSET_SECS: i64 = -5 * 3600;
const EDT_OFFSET_SECS: i64 = -4 * 3600;

/// US Equity market trading session phase in `America/New_York`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum UsMarketSession {
    PreMarket,
    Regular,
    AfterHours,
    Closed,
}

/// Proleptic Gregorian civil date in `America/New_York`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct EtDate {
    pub year: i32,
    pub month: u8,
    pub day: u8,
}

/// Howard Hinnant's civil-from-days algorithm (`1970-01-01` -> day 0).
fn civil_from_days(z: i64) -> EtDate {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if m <= 2 { 1 } else { 0 };
    EtDate {
        year: year as i32,
        month: m as u8,
        day: d as u8,
    }
}

/// Howard Hinnant's days-from-civil algorithm (`1970-01-01` -> 0).
fn days_from_civil(y: i32, m: u8, d: u8) -> i64 {
    let y = y as i64 - if m <= 2 { 1 } else { 0 };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let m = m as i64;
    let d = d as i64;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// ISO weekday where Monday = 0, Sunday = 6.
fn weekday_mon0(date: EtDate) -> i64 {
    let days = days_from_civil(date.year, date.month, date.day);
    (days + 3).rem_euclid(7)
}

fn add_days(date: EtDate, delta_days: i64) -> EtDate {
    civil_from_days(days_from_civil(date.year, date.month, date.day) + delta_days)
}

fn nth_weekday_of_month(year: i32, month: u8, target_wd_mon0: i64, nth: u8) -> u8 {
    let first_wd = weekday_mon0(EtDate {
        year,
        month,
        day: 1,
    });
    let offset = (target_wd_mon0 - first_wd).rem_euclid(7) as u8;
    1 + offset + (nth - 1) * 7
}

fn last_weekday_of_month(year: i32, month: u8, target_wd_mon0: i64) -> u8 {
    let days_in_month = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            let leap = (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
            if leap { 29 } else { 28 }
        }
        _ => unreachable!(),
    };
    let last_wd = weekday_mon0(EtDate {
        year,
        month,
        day: days_in_month,
    });
    let back = (last_wd - target_wd_mon0).rem_euclid(7) as u8;
    days_in_month - back
}

fn us_transitions_utc(year: i32) -> (i64, i64) {
    let (start_month, start_day, end_month, end_day) = if year >= 2007 {
        (
            3u8,
            nth_weekday_of_month(year, 3, 6, 2),
            11u8,
            nth_weekday_of_month(year, 11, 6, 1),
        )
    } else {
        (
            4u8,
            nth_weekday_of_month(year, 4, 6, 1),
            10u8,
            last_weekday_of_month(year, 10, 6),
        )
    };
    // Spring forward at 02:00 EST; fall back at 02:00 EDT.
    let start_utc = days_from_civil(year, start_month, start_day) * DAY_SECS + 2 * 3600 - EST_OFFSET_SECS;
    let end_utc = days_from_civil(year, end_month, end_day) * DAY_SECS + 2 * 3600 - EDT_OFFSET_SECS;
    (start_utc, end_utc)
}

/// UTC offset in seconds for `America/New_York` at UTC epoch seconds `utc_secs`.
pub fn et_utc_offset_secs(utc_secs: i64) -> i64 {
    let approx = civil_from_days(utc_secs.div_euclid(DAY_SECS));
    let (start_utc, end_utc) = us_transitions_utc(approx.year);
    if utc_secs >= start_utc && utc_secs < end_utc {
        EDT_OFFSET_SECS
    } else {
        EST_OFFSET_SECS
    }
}

/// Convert UTC milliseconds to `America/New_York` `(date, hour, minute, second)`.
pub fn utc_ms_to_et(utc_ms: i64) -> (EtDate, u32, u32, u32) {
    let utc_secs = utc_ms.div_euclid(SEC_MS);
    let offset = et_utc_offset_secs(utc_secs);
    let local_secs = utc_secs + offset;
    let days = local_secs.div_euclid(DAY_SECS);
    let sod = local_secs.rem_euclid(DAY_SECS) as u32;
    let hour = sod / 3600;
    let minute = (sod % 3600) / 60;
    let second = sod % 60;
    (civil_from_days(days), hour, minute, second)
}

/// Convert an `America/New_York` local `(year, month, day, hour, minute, second)` to UTC ms.
pub fn et_to_utc_ms(year: i32, month: u8, day: u8, hour: u32, minute: u32, second: u32) -> i64 {
    let local_secs = days_from_civil(year, month, day) * DAY_SECS
        + (hour as i64) * 3600
        + (minute as i64) * 60
        + (second as i64);
    let (start_utc, end_utc) = us_transitions_utc(year);
    let cand_edt = local_secs - EDT_OFFSET_SECS;
    let cand_est = local_secs - EST_OFFSET_SECS;
    let utc_secs = if cand_edt >= start_utc && cand_edt < end_utc {
        cand_edt
    } else {
        cand_est
    };
    utc_secs * SEC_MS
}

/// Anonymous Gregorian Easter Sunday for `year`.
fn easter_sunday(year: i32) -> EtDate {
    let a = year.rem_euclid(19);
    let b = year.div_euclid(100);
    let c = year.rem_euclid(100);
    let d = b.div_euclid(4);
    let e = b.rem_euclid(4);
    let f = (b + 8).div_euclid(25);
    let g = (b - f + 1).div_euclid(3);
    let h = (19 * a + b - d - g + 15).rem_euclid(30);
    let i = c.div_euclid(4);
    let k = c.rem_euclid(4);
    let l = (32 + 2 * e + 2 * i - h - k).rem_euclid(7);
    let m = (a + 11 * h + 22 * l).div_euclid(451);
    let month = (h + l - 7 * m + 114).div_euclid(31) as u8;
    let day = ((h + l - 7 * m + 114).rem_euclid(31) + 1) as u8;
    EtDate { year, month, day }
}

fn observed_weekend_holiday(year: i32, month: u8, day: u8) -> EtDate {
    let date = EtDate { year, month, day };
    match weekday_mon0(date) {
        5 => add_days(date, -1), // Saturday -> Friday
        6 => add_days(date, 1),  // Sunday -> Monday
        _ => date,
    }
}

/// Return `true` if `date` is an observed NYSE/NASDAQ full-day market holiday.
pub fn is_us_market_holiday(date: EtDate) -> bool {
    let y = date.year;

    // NYSE Rule 7.2: New Year's Day is observed on Monday if Sunday, not observed on Friday if Saturday.
    let new_years = EtDate {
        year: y,
        month: 1,
        day: 1,
    };
    let ny_observed = if weekday_mon0(new_years) == 6 {
        EtDate {
            year: y,
            month: 1,
            day: 2,
        }
    } else {
        new_years
    };
    if weekday_mon0(new_years) != 5 && date == ny_observed {
        return true;
    }

    // MLK Jr. Day: 3rd Monday of January (observed by NYSE since 1998).
    if y >= 1998
        && date
            == (EtDate {
                year: y,
                month: 1,
                day: nth_weekday_of_month(y, 1, 0, 3),
            })
    {
        return true;
    }

    // Presidents' Day: 3rd Monday of February.
    if date
        == (EtDate {
            year: y,
            month: 2,
            day: nth_weekday_of_month(y, 2, 0, 3),
        })
    {
        return true;
    }

    // Good Friday: 2 days before Easter Sunday.
    if date == add_days(easter_sunday(y), -2) {
        return true;
    }

    // Memorial Day: Last Monday of May.
    if date
        == (EtDate {
            year: y,
            month: 5,
            day: last_weekday_of_month(y, 5, 0),
        })
    {
        return true;
    }

    // Juneteenth National Independence Day: June 19 observed (NYSE from 2022).
    if y >= 2022 && date == observed_weekend_holiday(y, 6, 19) {
        return true;
    }

    // Independence Day: July 4 observed.
    if date == observed_weekend_holiday(y, 7, 4) {
        return true;
    }

    // Labor Day: 1st Monday of September.
    if date
        == (EtDate {
            year: y,
            month: 9,
            day: nth_weekday_of_month(y, 9, 0, 1),
        })
    {
        return true;
    }

    // Thanksgiving Day: 4th Thursday of November.
    if date
        == (EtDate {
            year: y,
            month: 11,
            day: nth_weekday_of_month(y, 11, 3, 4),
        })
    {
        return true;
    }

    // Christmas Day: December 25 observed.
    if date == observed_weekend_holiday(y, 12, 25) {
        return true;
    }

    false
}

/// Return `true` if `date` is a US equity trading day (weekday and not a market holiday).
pub fn is_us_trading_day(date: EtDate) -> bool {
    let wd = weekday_mon0(date);
    wd < 5 && !is_us_market_holiday(date)
}

/// Classify a UTC millisecond timestamp into a US Equity market session in `America/New_York`.
pub fn classify_us_equity_session(utc_ms: i64) -> UsMarketSession {
    let (date, hour, minute, second) = utc_ms_to_et(utc_ms);
    if !is_us_trading_day(date) {
        return UsMarketSession::Closed;
    }
    let sod = hour * 3600 + minute * 60 + second;
    const PRE_START: u32 = 4 * 3600;
    const REG_START: u32 = 9 * 3600 + 30 * 60;
    const REG_END: u32 = 16 * 3600;
    const AFTER_END: u32 = 20 * 3600;

    if (PRE_START..REG_START).contains(&sod) {
        UsMarketSession::PreMarket
    } else if (REG_START..REG_END).contains(&sod) {
        UsMarketSession::Regular
    } else if (REG_END..AFTER_END).contains(&sod) {
        UsMarketSession::AfterHours
    } else {
        UsMarketSession::Closed
    }
}

/// Return `true` if `utc_ms` lies within the explicit [`AlpacaDataFeed`] trading-day window:
/// - `AlpacaDataFeed::Iex`: `08:00..17:00 America/New_York` on open US trading days
/// - `AlpacaDataFeed::Sip`: `04:00..20:00 America/New_York` on open US trading days
pub fn is_within_feed_hours(feed: AlpacaDataFeed, utc_ms: i64) -> bool {
    let (date, hour, minute, second) = utc_ms_to_et(utc_ms);
    if !is_us_trading_day(date) {
        return false;
    }
    let sod = hour * 3600 + minute * 60 + second;
    match feed {
        AlpacaDataFeed::Iex => (8 * 3600..17 * 3600).contains(&sod),
        AlpacaDataFeed::Sip => (4 * 3600..20 * 3600).contains(&sod),
    }
}

/// Build the `talaria_contracts::SessionTrait` describing the active [`AlpacaDataFeed`].
pub fn session_trait_for_feed(feed: AlpacaDataFeed) -> SessionTrait {
    let (pre_start, after_end) = match feed {
        AlpacaDataFeed::Iex => ("08:00", "17:00"),
        AlpacaDataFeed::Sip => ("04:00", "20:00"),
    };
    SessionTrait {
        session_kind: "us_equity".to_owned(),
        timezone: Some("America/New_York".to_owned()),
        day_start: "09:30".to_owned(),
        sessions: vec![
            TradingSession {
                name: "pre_market".to_owned(),
                start: pre_start.to_owned(),
                end: "09:30".to_owned(),
                timezone: Some("America/New_York".to_owned()),
            },
            TradingSession {
                name: "regular".to_owned(),
                start: "09:30".to_owned(),
                end: "16:00".to_owned(),
                timezone: Some("America/New_York".to_owned()),
            },
            TradingSession {
                name: "after_hours".to_owned(),
                start: "16:00".to_owned(),
                end: after_end.to_owned(),
                timezone: Some("America/New_York".to_owned()),
            },
        ],
    }
}
