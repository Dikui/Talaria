//! Zero-I/O proleptic Gregorian calendar and timezone offset/DST arithmetic
//! matching Python `zoneinfo.ZoneInfo` for session boundary calculations.

const SEC_MS: i64 = 1_000;
const DAY_SECS: i64 = 86_400;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CivilDate {
    pub year: i32,
    pub month: u8,
    pub day: u8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeZoneRule {
    FixedSeconds(i64),
    UsEastern,   // America/New_York
    UsCentral,   // America/Chicago
    UsPacific,   // America/Los_Angeles
    EuLondon,    // Europe/London
    EuCentral,   // Europe/Berlin, Europe/Zurich, Europe/Paris
}

impl TimeZoneRule {
    /// Resolve timezone name to a rule, falling back to UTC on unknown names (matching Python `_timezone`).
    pub fn from_name(name: &str) -> Self {
        match name.trim() {
            "UTC" | "Etc/UTC" | "GMT" | "Etc/GMT" | "Z" => Self::FixedSeconds(0),
            "America/New_York" | "US/Eastern" => Self::UsEastern,
            "America/Chicago" | "US/Central" => Self::UsCentral,
            "America/Los_Angeles" | "US/Pacific" => Self::UsPacific,
            "Europe/London" | "GB" => Self::EuLondon,
            "Europe/Berlin" | "Europe/Zurich" | "Europe/Paris" | "CET" => Self::EuCentral,
            "Asia/Shanghai" | "Asia/Hong_Kong" | "Asia/Singapore" | "Asia/Taipei" => {
                Self::FixedSeconds(8 * 3600)
            }
            "Asia/Tokyo" | "Asia/Seoul" => Self::FixedSeconds(9 * 3600),
            other => parse_fixed_offset(other).unwrap_or(Self::FixedSeconds(0)),
        }
    }

    /// UTC offset in seconds at a given UTC instant (epoch seconds).
    pub fn offset_at_utc_secs(self, utc_secs: i64) -> i64 {
        match self {
            Self::FixedSeconds(off) => off,
            Self::UsEastern => us_dst_offset(utc_secs, -5 * 3600, -4 * 3600),
            Self::UsCentral => us_dst_offset(utc_secs, -6 * 3600, -5 * 3600),
            Self::UsPacific => us_dst_offset(utc_secs, -8 * 3600, -7 * 3600),
            Self::EuLondon => eu_dst_offset(utc_secs, 0, 3600),
            Self::EuCentral => eu_dst_offset(utc_secs, 3600, 2 * 3600),
        }
    }

    /// Convert local `(date, hour, minute)` to UTC milliseconds (`fold = 0` matching Python default).
    pub fn local_to_utc_ms(self, date: CivilDate, hour: u32, minute: u32) -> i64 {
        let local_secs = days_from_civil(date.year, date.month, date.day) * DAY_SECS
            + (hour as i64) * 3600
            + (minute as i64) * 60;
        let utc_secs = match self {
            Self::FixedSeconds(off) => local_secs - off,
            Self::UsEastern => us_local_to_utc_secs(date, local_secs, -5 * 3600, -4 * 3600),
            Self::UsCentral => us_local_to_utc_secs(date, local_secs, -6 * 3600, -5 * 3600),
            Self::UsPacific => us_local_to_utc_secs(date, local_secs, -8 * 3600, -7 * 3600),
            Self::EuLondon => eu_local_to_utc_secs(date, local_secs, 0, 3600),
            Self::EuCentral => eu_local_to_utc_secs(date, local_secs, 3600, 2 * 3600),
        };
        utc_secs * SEC_MS
    }

    /// Convert UTC milliseconds to local `(CivilDate, seconds_since_midnight)`.
    pub fn utc_ms_to_local(self, utc_ms: i64) -> (CivilDate, u32) {
        let utc_secs = utc_ms.div_euclid(SEC_MS);
        let offset = self.offset_at_utc_secs(utc_secs);
        let local_secs = utc_secs + offset;
        let days = local_secs.div_euclid(DAY_SECS);
        let sod = local_secs.rem_euclid(DAY_SECS) as u32;
        (civil_from_days(days), sod)
    }
}

fn parse_fixed_offset(s: &str) -> Option<TimeZoneRule> {
    let rest = s.strip_prefix("UTC").or_else(|| s.strip_prefix("GMT")).unwrap_or(s);
    if rest.is_empty() {
        return Some(TimeZoneRule::FixedSeconds(0));
    }
    let (sign, digits) = if let Some(r) = rest.strip_prefix('+') {
        (1i64, r)
    } else {
        let r = rest.strip_prefix('-')?;
        (-1i64, r)
    };
    let (hh_str, mm_str) = digits.split_once(':').unwrap_or((digits, "0"));
    let hh: i64 = hh_str.parse().ok()?;
    let mm: i64 = mm_str.parse().ok()?;
    if !(0..=23).contains(&hh) || !(0..=59).contains(&mm) {
        return None;
    }
    Some(TimeZoneRule::FixedSeconds(sign * (hh * 3600 + mm * 60)))
}

/// Howard Hinnant's civil-from-days algorithm (`1970-01-01` -> day 0).
pub fn civil_from_days(z: i64) -> CivilDate {
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
    CivilDate {
        year: year as i32,
        month: m as u8,
        day: d as u8,
    }
}

/// Howard Hinnant's days-from-civil algorithm (`1970-01-01` -> 0).
pub fn days_from_civil(y: i32, m: u8, d: u8) -> i64 {
    let y = y as i64 - if m <= 2 { 1 } else { 0 };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let m = m as i64;
    let d = d as i64;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// ISO weekday where Monday = 0, Sunday = 6 (matches Python `date.weekday()`).
pub fn weekday_mon0(date: CivilDate) -> i64 {
    let days = days_from_civil(date.year, date.month, date.day);
    // 1970-01-01 was Thursday (3).
    (days + 3).rem_euclid(7)
}

pub fn add_days(date: CivilDate, delta_days: i64) -> CivilDate {
    civil_from_days(days_from_civil(date.year, date.month, date.day) + delta_days)
}

fn nth_sunday_of_month(year: i32, month: u8, nth: u8) -> u8 {
    let first_wd = weekday_mon0(CivilDate {
        year,
        month,
        day: 1,
    });
    let sunday_wd = 6i64;
    let offset = (sunday_wd - first_wd).rem_euclid(7) as u8;
    1 + offset + (nth - 1) * 7
}

fn last_sunday_of_month(year: i32, month: u8) -> u8 {
    let days_in_month = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            let leap = (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
            if leap { 29 } else { 28 }
        }
        _ => unreachable!(),
    };
    let last_wd = weekday_mon0(CivilDate {
        year,
        month,
        day: days_in_month,
    });
    let sunday_wd = 6i64;
    let back = (last_wd - sunday_wd).rem_euclid(7) as u8;
    days_in_month - back
}

fn us_transitions_utc(year: i32, std_off: i64, dst_off: i64) -> (i64, i64) {
    let (start_month, start_day, end_month, end_day) = if year >= 2007 {
        (
            3u8,
            nth_sunday_of_month(year, 3, 2),
            11u8,
            nth_sunday_of_month(year, 11, 1),
        )
    } else {
        (
            4u8,
            nth_sunday_of_month(year, 4, 1),
            10u8,
            last_sunday_of_month(year, 10),
        )
    };
    // Spring forward at 02:00 standard local time; fall back at 02:00 DST local time.
    let start_utc = days_from_civil(year, start_month, start_day) * DAY_SECS + 2 * 3600 - std_off;
    let end_utc = days_from_civil(year, end_month, end_day) * DAY_SECS + 2 * 3600 - dst_off;
    (start_utc, end_utc)
}

fn us_dst_offset(utc_secs: i64, std_off: i64, dst_off: i64) -> i64 {
    let approx_date = civil_from_days(utc_secs.div_euclid(DAY_SECS));
    let (start_utc, end_utc) = us_transitions_utc(approx_date.year, std_off, dst_off);
    if utc_secs >= start_utc && utc_secs < end_utc {
        dst_off
    } else {
        std_off
    }
}

fn us_local_to_utc_secs(date: CivilDate, local_secs: i64, std_off: i64, dst_off: i64) -> i64 {
    let (start_utc, end_utc) = us_transitions_utc(date.year, std_off, dst_off);
    // Test fold=0 (earlier instant): try DST first when during ambiguous fold, or STD before spring.
    let cand_dst = local_secs - dst_off;
    let cand_std = local_secs - std_off;
    if cand_dst >= start_utc && cand_dst < end_utc {
        cand_dst
    } else {
        cand_std
    }
}

fn eu_transitions_utc(year: i32) -> (i64, i64) {
    let start_day = last_sunday_of_month(year, 3);
    let end_day = last_sunday_of_month(year, 10);
    let start_utc = days_from_civil(year, 3, start_day) * DAY_SECS + 3600;
    let end_utc = days_from_civil(year, 10, end_day) * DAY_SECS + 3600;
    (start_utc, end_utc)
}

fn eu_dst_offset(utc_secs: i64, std_off: i64, dst_off: i64) -> i64 {
    let approx_date = civil_from_days(utc_secs.div_euclid(DAY_SECS));
    let (start_utc, end_utc) = eu_transitions_utc(approx_date.year);
    if utc_secs >= start_utc && utc_secs < end_utc {
        dst_off
    } else {
        std_off
    }
}

fn eu_local_to_utc_secs(date: CivilDate, local_secs: i64, std_off: i64, dst_off: i64) -> i64 {
    let (start_utc, end_utc) = eu_transitions_utc(date.year);
    let cand_dst = local_secs - dst_off;
    let cand_std = local_secs - std_off;
    if cand_dst >= start_utc && cand_dst < end_utc {
        cand_dst
    } else {
        cand_std
    }
}

pub fn parse_hm(value: &str) -> (u32, u32) {
    let (h_str, m_str) = value.split_once(':').unwrap_or((value, ""));
    let h = h_str.parse::<u32>().unwrap_or(0);
    let m = m_str.parse::<u32>().unwrap_or(0);
    (h, m)
}

pub fn day_start_ms(ts_ms: i64, tz: TimeZoneRule, day_start: &str) -> i64 {
    let (hour, minute) = parse_hm(day_start);
    let (date, sod) = tz.utc_ms_to_local(ts_ms);
    let boundary_sod = hour * 3600 + minute * 60;
    let effective_date = if sod < boundary_sod {
        add_days(date, -1)
    } else {
        date
    };
    tz.local_to_utc_ms(effective_date, hour, minute)
}

pub fn previous_day_start_ms(current_day_start_ms: i64, tz: TimeZoneRule, day_start: &str) -> i64 {
    let (hour, minute) = parse_hm(day_start);
    let (date, _) = tz.utc_ms_to_local(current_day_start_ms);
    let prev_date = add_days(date, -1);
    tz.local_to_utc_ms(prev_date, hour, minute)
}

pub fn month_start_ms(ts_ms: i64, tz: TimeZoneRule, day_start: &str) -> i64 {
    let (hour, minute) = parse_hm(day_start);
    let (date, _) = tz.utc_ms_to_local(ts_ms);
    tz.local_to_utc_ms(
        CivilDate {
            year: date.year,
            month: date.month,
            day: 1,
        },
        hour,
        minute,
    )
}

pub fn week_start_ms(ts_ms: i64, tz: TimeZoneRule, day_start: &str) -> i64 {
    let (hour, minute) = parse_hm(day_start);
    let (date, _) = tz.utc_ms_to_local(ts_ms);
    let wd = weekday_mon0(date);
    let monday = add_days(date, -wd);
    tz.local_to_utc_ms(monday, hour, minute)
}

pub fn latest_session_window(
    cutoff_ts: i64,
    start: &str,
    end: &str,
    tz: TimeZoneRule,
) -> Option<(i64, i64)> {
    let (today, _) = tz.utc_ms_to_local(cutoff_ts);
    let (sh, sm) = parse_hm(start);
    let (eh, em) = parse_hm(end);
    for offset in 0..=2 {
        let candidate = add_days(today, -offset);
        let start_ms = tz.local_to_utc_ms(candidate, sh, sm);
        let mut end_ms = tz.local_to_utc_ms(candidate, eh, em);
        if end_ms <= start_ms {
            end_ms = tz.local_to_utc_ms(add_days(candidate, 1), eh, em);
        }
        if end_ms <= cutoff_ts {
            return Some((start_ms, end_ms));
        }
    }
    None
}
