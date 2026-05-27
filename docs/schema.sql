-- TG Monitor Platform - Database Schema
-- MySQL 5.7+ / 8.0, charset utf8mb4
--
-- Usage:
--   1. Create database:  CREATE DATABASE tg_monitor CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--   2. Import schema:    mysql -u root -p tg_monitor < docs/schema.sql
--   Or just run:         python -m app.cli init-db

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- -----------------------------------------------------------
-- 应用配置键值表
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `app_settings` (
  `key`       VARCHAR(100) NOT NULL,
  `value`     TEXT          DEFAULT NULL,
  `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 监控群组/频道
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `monitored_chats` (
  `id`                      INT          NOT NULL AUTO_INCREMENT,
  `telegram_id`             BIGINT       NOT NULL,
  `access_hash`             BIGINT       DEFAULT NULL,
  `title`                   VARCHAR(255) NOT NULL,
  `username`                VARCHAR(255) DEFAULT NULL,
  `chat_type`               VARCHAR(50)  NOT NULL DEFAULT 'unknown',
  `is_active`               TINYINT(1)   NOT NULL DEFAULT 1,
  `last_synced_message_id`  INT          DEFAULT NULL,
  `last_message_at`         DATETIME     DEFAULT NULL,
  `created_at`              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_telegram_id` (`telegram_id`),
  KEY `idx_monitored_chats_title` (`title`),
  KEY `idx_monitored_chats_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- Telegram 用户
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `telegram_users` (
  `id`          INT          NOT NULL AUTO_INCREMENT,
  `telegram_id` BIGINT       NOT NULL,
  `username`    VARCHAR(255) DEFAULT NULL,
  `first_name`  VARCHAR(255) DEFAULT NULL,
  `last_name`   VARCHAR(255) DEFAULT NULL,
  `is_bot`      TINYINT(1)   NOT NULL DEFAULT 0,
  `about`       TEXT          DEFAULT NULL,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_telegram_id` (`telegram_id`),
  KEY `idx_telegram_users_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 消息
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `messages` (
  `id`                  INT          NOT NULL AUTO_INCREMENT,
  `chat_id`             INT          NOT NULL,
  `sender_user_id`      INT          DEFAULT NULL,
  `telegram_message_id` INT          NOT NULL,
  `message_date`        DATETIME     NOT NULL,
  `edit_date`           DATETIME     DEFAULT NULL,
  `raw_text`            TEXT          DEFAULT NULL,
  `normalized_text`     TEXT          DEFAULT NULL,
  `reply_to_msg_id`     INT          DEFAULT NULL,
  `views`               INT          DEFAULT NULL,
  `forwards`            INT          DEFAULT NULL,
  `has_media`           TINYINT(1)   NOT NULL DEFAULT 0,
  `media_type`          VARCHAR(50)  DEFAULT NULL,
  `meta_json`           JSON         DEFAULT NULL,
  `created_at`          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_chat_message` (`chat_id`, `telegram_message_id`),
  KEY `idx_messages_message_date` (`message_date`),
  KEY `idx_messages_chat_id_id` (`chat_id`, `id`),
  KEY `idx_messages_chat_tg_msg` (`chat_id`, `telegram_message_id`),
  KEY `idx_messages_chat_date` (`chat_id`, `message_date`),
  KEY `idx_messages_sender_user_id` (`sender_user_id`),
  CONSTRAINT `fk_messages_chat`   FOREIGN KEY (`chat_id`)        REFERENCES `monitored_chats` (`id`),
  CONSTRAINT `fk_messages_sender` FOREIGN KEY (`sender_user_id`) REFERENCES `telegram_users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 消息关键词
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `message_keywords` (
  `id`         INT         NOT NULL AUTO_INCREMENT,
  `message_id` INT         NOT NULL,
  `keyword`    VARCHAR(100) NOT NULL,
  `weight`     INT         NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  KEY `idx_message_keywords_message_id` (`message_id`),
  KEY `idx_keyword_keyword` (`keyword`),
  CONSTRAINT `fk_keywords_message` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 同步任务记录
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `sync_runs` (
  `id`          INT         NOT NULL AUTO_INCREMENT,
  `chat_id`     INT         DEFAULT NULL,
  `run_type`    VARCHAR(50) NOT NULL,
  `status`      VARCHAR(30) NOT NULL,
  `message`     TEXT        DEFAULT NULL,
  `started_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `finished_at` DATETIME    DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_sync_runs_run_type` (`run_type`),
  KEY `idx_sync_runs_status` (`status`),
  CONSTRAINT `fk_sync_runs_chat` FOREIGN KEY (`chat_id`) REFERENCES `monitored_chats` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- AI 摘要
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_summaries` (
  `id`               INT         NOT NULL AUTO_INCREMENT,
  `chat_id`          INT         NOT NULL,
  `message_count`    INT         NOT NULL DEFAULT 0,
  `start_message_id` INT         NOT NULL DEFAULT 0,
  `end_message_id`   INT         NOT NULL DEFAULT 0,
  `summary_text`     TEXT        DEFAULT NULL,
  `extracted_urls`   JSON        DEFAULT NULL,
  `status`           VARCHAR(30) NOT NULL DEFAULT 'pending',
  `error_message`    TEXT        DEFAULT NULL,
  `triggered_at`     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `completed_at`     DATETIME    DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ai_summaries_chat_id` (`chat_id`),
  KEY `idx_ai_summaries_status` (`status`),
  KEY `idx_summary_chat_status` (`chat_id`, `status`),
  KEY `idx_summary_chat_status_end` (`chat_id`, `status`, `end_message_id`),
  CONSTRAINT `fk_summaries_chat` FOREIGN KEY (`chat_id`) REFERENCES `monitored_chats` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- AI 提取的 URL
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_urls` (
  `id`                INT          NOT NULL AUTO_INCREMENT,
  `url`               TEXT         NOT NULL,
  `url_hash`          VARCHAR(64)  NOT NULL,
  `category`          VARCHAR(20)  NOT NULL,
  `domain`            VARCHAR(255) DEFAULT NULL,
  `appearance_count`  INT          DEFAULT 1,
  `chat_ids_seen`     JSON         DEFAULT NULL,
  `reputation_score`  FLOAT        DEFAULT NULL,
  `first_seen_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_urls_url_hash` (`url_hash`),
  KEY `idx_ai_urls_category` (`category`),
  KEY `idx_ai_urls_domain` (`domain`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- URL 出现记录
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_url_appearances` (
  `id`          INT      NOT NULL AUTO_INCREMENT,
  `url_id`      INT      NOT NULL,
  `chat_id`     INT      NOT NULL,
  `summary_id`  INT      DEFAULT NULL,
  `seen_at`     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_url_appearances_url_id` (`url_id`),
  KEY `idx_url_appearances_chat_id` (`chat_id`),
  KEY `idx_url_appearance_chat` (`url_id`, `chat_id`),
  KEY `idx_url_appearance_date` (`seen_at`),
  CONSTRAINT `fk_url_appearances_url`     FOREIGN KEY (`url_id`)     REFERENCES `ai_urls` (`id`),
  CONSTRAINT `fk_url_appearances_chat`    FOREIGN KEY (`chat_id`)    REFERENCES `monitored_chats` (`id`),
  CONSTRAINT `fk_url_appearances_summary` FOREIGN KEY (`summary_id`) REFERENCES `ai_summaries` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- AI 提取的商品
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_products` (
  `id`              INT          NOT NULL AUTO_INCREMENT,
  `chat_id`         INT          NOT NULL,
  `summary_id`      INT          DEFAULT NULL,
  `product_name`    VARCHAR(255) NOT NULL,
  `price_amount`    FLOAT        DEFAULT NULL,
  `price_currency`  VARCHAR(20)  NOT NULL DEFAULT 'CNY',
  `seller_contact`  VARCHAR(255) DEFAULT NULL,
  `status`          VARCHAR(20)  NOT NULL DEFAULT 'available',
  `first_seen_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen_at`    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ai_products_chat_id` (`chat_id`),
  KEY `idx_ai_products_summary_id` (`summary_id`),
  KEY `idx_ai_products_product_name` (`product_name`),
  KEY `idx_ai_products_status` (`status`),
  KEY `idx_product_chat_name` (`chat_id`, `product_name`),
  CONSTRAINT `fk_products_chat`    FOREIGN KEY (`chat_id`)    REFERENCES `monitored_chats` (`id`),
  CONSTRAINT `fk_products_summary` FOREIGN KEY (`summary_id`) REFERENCES `ai_summaries` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- AI 提取的联系方式
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_contacts` (
  `id`             INT          NOT NULL AUTO_INCREMENT,
  `chat_id`        INT          NOT NULL,
  `summary_id`     INT          DEFAULT NULL,
  `contact_type`   VARCHAR(30)  NOT NULL,
  `contact_value`  VARCHAR(255) NOT NULL,
  `context`        TEXT         DEFAULT NULL,
  `first_seen_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ai_contacts_chat_id` (`chat_id`),
  KEY `idx_ai_contacts_summary_id` (`summary_id`),
  KEY `idx_ai_contacts_contact_type` (`contact_type`),
  KEY `idx_ai_contacts_contact_value` (`contact_value`),
  KEY `idx_contact_chat_type_value` (`chat_id`, `contact_type`, `contact_value`),
  CONSTRAINT `fk_contacts_chat`    FOREIGN KEY (`chat_id`)    REFERENCES `monitored_chats` (`id`),
  CONSTRAINT `fk_contacts_summary` FOREIGN KEY (`summary_id`) REFERENCES `ai_summaries` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 告警规则
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `alert_rules` (
  `id`              INT          NOT NULL AUTO_INCREMENT,
  `name`            VARCHAR(100) NOT NULL,
  `pattern`         TEXT         NOT NULL,
  `pattern_type`    VARCHAR(20)  NOT NULL DEFAULT 'keyword',
  `is_active`       TINYINT(1)   NOT NULL DEFAULT 1,
  `notify_web`      TINYINT(1)   NOT NULL DEFAULT 1,
  `notify_telegram` TINYINT(1)   NOT NULL DEFAULT 0,
  `chat_ids_filter` JSON         DEFAULT NULL,
  `created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 告警匹配记录
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `alert_matches` (
  `id`           INT      NOT NULL AUTO_INCREMENT,
  `rule_id`      INT      NOT NULL,
  `message_id`   INT      NOT NULL,
  `chat_id`      INT      NOT NULL,
  `matched_text` TEXT     DEFAULT NULL,
  `matched_at`   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `is_read`      TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_alert_matches_rule_id` (`rule_id`),
  KEY `idx_alert_matches_message_id` (`message_id`),
  KEY `idx_alert_matches_chat_id` (`chat_id`),
  KEY `idx_alert_match_rule_date` (`rule_id`, `matched_at`),
  CONSTRAINT `fk_alert_matches_rule`    FOREIGN KEY (`rule_id`)    REFERENCES `alert_rules` (`id`),
  CONSTRAINT `fk_alert_matches_message` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`),
  CONSTRAINT `fk_alert_matches_chat`    FOREIGN KEY (`chat_id`)    REFERENCES `monitored_chats` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;
