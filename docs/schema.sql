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
-- Telegram 自动加群目标
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `telegram_join_targets` (
  `id`                   INT          NOT NULL AUTO_INCREMENT,
  `source`               VARCHAR(500) NOT NULL,
  `normalized_key`       VARCHAR(255) NOT NULL,
  `target_type`          VARCHAR(30)  NOT NULL DEFAULT 'unknown',
  `title`                VARCHAR(255) DEFAULT NULL,
  `status`               VARCHAR(30)  NOT NULL DEFAULT 'pending',
  `attempt_count`        INT          NOT NULL DEFAULT 0,
  `last_error`           TEXT         DEFAULT NULL,
  `last_attempt_at`      DATETIME     DEFAULT NULL,
  `next_attempt_at`      DATETIME     DEFAULT NULL,
  `joined_at`            DATETIME     DEFAULT NULL,
  `resolved_telegram_id` BIGINT       DEFAULT NULL,
  `monitored_chat_id`    INT          DEFAULT NULL,
  `created_at`           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_join_target_key` (`normalized_key`),
  KEY `idx_join_target_status_next` (`status`, `next_attempt_at`),
  KEY `idx_join_target_monitored_chat` (`monitored_chat_id`),
  KEY `idx_join_targets_normalized_key` (`normalized_key`),
  KEY `idx_join_targets_target_type` (`target_type`),
  KEY `idx_join_targets_status` (`status`),
  KEY `idx_join_targets_resolved_telegram_id` (`resolved_telegram_id`),
  CONSTRAINT `fk_join_targets_monitored_chat` FOREIGN KEY (`monitored_chat_id`) REFERENCES `monitored_chats` (`id`)
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
  `classification_status` VARCHAR(20) NOT NULL DEFAULT 'pending',
  `primary_category_id`   INT         DEFAULT NULL,
  `classification_run_id` INT         DEFAULT NULL,
  `classified_at`         DATETIME    DEFAULT NULL,
  `classification_error`  TEXT        DEFAULT NULL,
  `first_seen_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_urls_url_hash` (`url_hash`),
  KEY `idx_ai_urls_category` (`category`),
  KEY `idx_ai_urls_domain` (`domain`),
  KEY `ix_ai_urls_classification_status` (`classification_status`),
  KEY `ix_ai_urls_primary_category_id` (`primary_category_id`),
  KEY `ix_ai_urls_classification_run_id` (`classification_run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- URL 动态分类
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_url_categories` (
  `id`          INT          NOT NULL AUTO_INCREMENT,
  `slug`        VARCHAR(80)  NOT NULL,
  `name`        VARCHAR(100) NOT NULL,
  `description` TEXT         DEFAULT NULL,
  `source`      VARCHAR(20)  NOT NULL DEFAULT 'ai',
  `is_active`   TINYINT(1)   NOT NULL DEFAULT 1,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_url_categories_slug` (`slug`),
  KEY `idx_ai_url_categories_slug` (`slug`),
  KEY `idx_ai_url_categories_name` (`name`),
  KEY `idx_ai_url_categories_source` (`source`),
  KEY `idx_ai_url_categories_is_active` (`is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_url_classification_runs` (
  `id`                 INT         NOT NULL AUTO_INCREMENT,
  `status`             VARCHAR(30) NOT NULL DEFAULT 'running',
  `batch_size`         INT         NOT NULL DEFAULT 50,
  `total_urls`         INT         NOT NULL DEFAULT 0,
  `processed_urls`     INT         NOT NULL DEFAULT 0,
  `created_categories` INT         NOT NULL DEFAULT 0,
  `prompt_version`     VARCHAR(50) DEFAULT NULL,
  `error_message`      TEXT        DEFAULT NULL,
  `started_at`         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `finished_at`        DATETIME    DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ai_url_classification_runs_status` (`status`),
  KEY `idx_ai_url_classification_runs_started_at` (`started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_url_classifications` (
  `id`          INT        NOT NULL AUTO_INCREMENT,
  `url_id`      INT        NOT NULL,
  `category_id` INT        NOT NULL,
  `run_id`      INT        DEFAULT NULL,
  `confidence`  FLOAT      DEFAULT NULL,
  `reason`      TEXT       DEFAULT NULL,
  `is_primary`  TINYINT(1) NOT NULL DEFAULT 1,
  `created_at`  DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_url_category` (`url_id`, `category_id`),
  KEY `idx_ai_url_classifications_url_id` (`url_id`),
  KEY `idx_ai_url_classifications_category_id` (`category_id`),
  KEY `idx_url_classification_run` (`run_id`),
  KEY `idx_ai_url_classifications_is_primary` (`is_primary`),
  CONSTRAINT `fk_url_classifications_url` FOREIGN KEY (`url_id`) REFERENCES `ai_urls` (`id`),
  CONSTRAINT `fk_url_classifications_category` FOREIGN KEY (`category_id`) REFERENCES `ai_url_categories` (`id`),
  CONSTRAINT `fk_url_classifications_run` FOREIGN KEY (`run_id`) REFERENCES `ai_url_classification_runs` (`id`)
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
-- Key 商线索分析批次
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_key_lead_runs` (
  `id`              INT         NOT NULL AUTO_INCREMENT,
  `status`          VARCHAR(30) NOT NULL DEFAULT 'running',
  `batch_size`      INT         NOT NULL DEFAULT 200,
  `total_messages`  INT         NOT NULL DEFAULT 0,
  `processed_leads` INT         NOT NULL DEFAULT 0,
  `start_message_id` INT        NOT NULL DEFAULT 0,
  `end_message_id`   INT        NOT NULL DEFAULT 0,
  `prompt_version`  VARCHAR(50) DEFAULT NULL,
  `error_message`   TEXT        DEFAULT NULL,
  `started_at`      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `finished_at`     DATETIME    DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ai_key_lead_runs_status` (`status`),
  KEY `idx_ai_key_lead_runs_start_message_id` (`start_message_id`),
  KEY `idx_ai_key_lead_runs_end_message_id` (`end_message_id`),
  KEY `idx_ai_key_lead_runs_started_at` (`started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- Key 商线索
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_key_leads` (
  `id`                  INT          NOT NULL AUTO_INCREMENT,
  `run_id`              INT          DEFAULT NULL,
  `message_id`          INT          NOT NULL,
  `chat_id`             INT          NOT NULL,
  `sender_user_id`      INT          DEFAULT NULL,
  `lead_type`           VARCHAR(30)  NOT NULL,
  `provider`            VARCHAR(60)  DEFAULT NULL,
  `product_name`        VARCHAR(255) DEFAULT NULL,
  `offer_text`          TEXT         DEFAULT NULL,
  `price_amount`        FLOAT        DEFAULT NULL,
  `price_currency`      VARCHAR(20)  DEFAULT NULL,
  `seller_contact`      VARCHAR(255) DEFAULT NULL,
  `seller_telegram_id`  BIGINT       DEFAULT NULL,
  `seller_username`     VARCHAR(255) DEFAULT NULL,
  `seller_display_name` VARCHAR(255) DEFAULT NULL,
  `confidence`          FLOAT        DEFAULT NULL,
  `reason`              TEXT         DEFAULT NULL,
  `source_text`         TEXT         DEFAULT NULL,
  `content_hash`        VARCHAR(64)  NOT NULL,
  `first_seen_at`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_key_lead_content_hash` (`content_hash`),
  KEY `idx_ai_key_leads_run_id` (`run_id`),
  KEY `idx_ai_key_leads_message_id` (`message_id`),
  KEY `idx_ai_key_leads_chat_id` (`chat_id`),
  KEY `idx_ai_key_leads_sender_user_id` (`sender_user_id`),
  KEY `idx_ai_key_leads_lead_type` (`lead_type`),
  KEY `idx_ai_key_leads_provider` (`provider`),
  KEY `idx_ai_key_leads_product_name` (`product_name`),
  KEY `idx_ai_key_leads_seller_contact` (`seller_contact`),
  KEY `ix_ai_key_leads_seller_telegram_id` (`seller_telegram_id`),
  KEY `ix_ai_key_leads_seller_username` (`seller_username`),
  KEY `idx_ai_key_leads_content_hash` (`content_hash`),
  KEY `idx_ai_key_leads_last_seen_at` (`last_seen_at`),
  KEY `idx_key_lead_provider_type` (`provider`, `lead_type`),
  KEY `idx_key_lead_chat_seen` (`chat_id`, `last_seen_at`),
  CONSTRAINT `fk_key_leads_run` FOREIGN KEY (`run_id`) REFERENCES `ai_key_lead_runs` (`id`),
  CONSTRAINT `fk_key_leads_message` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`),
  CONSTRAINT `fk_key_leads_chat` FOREIGN KEY (`chat_id`) REFERENCES `monitored_chats` (`id`),
  CONSTRAINT `fk_key_leads_sender` FOREIGN KEY (`sender_user_id`) REFERENCES `telegram_users` (`id`)
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

-- -----------------------------------------------------------
-- 消息编辑历史
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `message_edits` (
  `id`         INT      NOT NULL AUTO_INCREMENT,
  `message_id` INT      NOT NULL,
  `old_text`   TEXT     DEFAULT NULL,
  `new_text`   TEXT     DEFAULT NULL,
  `edit_date`  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_message_edits_message_id` (`message_id`),
  KEY `idx_message_edits_edit_date` (`edit_date`),
  CONSTRAINT `fk_message_edits_message` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 消息反应（点赞/表情）
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `message_reactions` (
  `id`             INT          NOT NULL AUTO_INCREMENT,
  `message_id`     INT          NOT NULL,
  `reaction_type`  VARCHAR(100) NOT NULL DEFAULT 'like',
  `count`          INT          NOT NULL DEFAULT 0,
  `updated_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_message_reaction` (`message_id`, `reaction_type`),
  KEY `idx_message_reactions_message_id` (`message_id`),
  CONSTRAINT `fk_message_reactions_message` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 内容指纹 / 跨群去重
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `message_fingerprints` (
  `id`                   INT          NOT NULL AUTO_INCREMENT,
  `message_id`           INT          NOT NULL,
  `fingerprint_hash`     VARCHAR(64)  NOT NULL,
  `similarity_hash`      VARCHAR(64)  DEFAULT NULL,
  `canonical_message_id` INT          DEFAULT NULL,
  `duplicate_count`      INT          NOT NULL DEFAULT 0,
  `created_at`           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_fingerprint_hash` (`fingerprint_hash`),
  KEY `idx_message_fprints_msg_id` (`message_id`),
  KEY `idx_message_fprints_similarity` (`similarity_hash`),
  KEY `idx_message_fprints_canonical` (`canonical_message_id`),
  CONSTRAINT `fk_fprints_message` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 浏览量历史（频道帖子）
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `message_views_history` (
  `id`          INT      NOT NULL AUTO_INCREMENT,
  `message_id`  INT      NOT NULL,
  `views`       INT      NOT NULL DEFAULT 0,
  `forwards`    INT      DEFAULT NULL,
  `recorded_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_views_history_message_id` (`message_id`),
  KEY `idx_views_history_recorded` (`recorded_at`),
  CONSTRAINT `fk_views_history_message` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 用户每日行为统计
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `user_daily_stats` (
  `id`              INT          NOT NULL AUTO_INCREMENT,
  `user_id`         INT          NOT NULL,
  `date`            DATE         NOT NULL,
  `message_count`   INT          NOT NULL DEFAULT 0,
  `word_count`      INT          NOT NULL DEFAULT 0,
  `media_count`     INT          NOT NULL DEFAULT 0,
  `active_hours_json` JSON       DEFAULT NULL,
  `top_chats_json`  JSON         DEFAULT NULL,
  `reputation_score` FLOAT       DEFAULT NULL,
  `updated_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_daily_stats` (`user_id`, `date`),
  KEY `idx_user_daily_stats_user_id` (`user_id`),
  KEY `idx_user_daily_stats_date` (`date`),
  CONSTRAINT `fk_user_daily_stats_user` FOREIGN KEY (`user_id`) REFERENCES `telegram_users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 商品价格历史
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `product_price_history` (
  `id`                INT          NOT NULL AUTO_INCREMENT,
  `product_id`        INT          NOT NULL,
  `price_amount`      FLOAT        DEFAULT NULL,
  `price_currency`    VARCHAR(20)  NOT NULL DEFAULT 'CNY',
  `source_message_id` INT          DEFAULT NULL,
  `seller_contact`    VARCHAR(255) DEFAULT NULL,
  `recorded_at`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_price_history_product_id` (`product_id`),
  KEY `idx_price_history_recorded` (`recorded_at`),
  CONSTRAINT `fk_price_history_product` FOREIGN KEY (`product_id`) REFERENCES `ai_products` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_price_history_message` FOREIGN KEY (`source_message_id`) REFERENCES `messages` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 市场情报结构化项（替代 JSON）
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `market_intelligence_items` (
  `id`                 INT          NOT NULL AUTO_INCREMENT,
  `summary_id`         INT          NOT NULL,
  `chat_id`            INT          NOT NULL,
  `item_type`          VARCHAR(30)  NOT NULL,
  `content`            TEXT         NOT NULL,
  `confidence`         FLOAT        DEFAULT NULL,
  `related_entities_json` JSON      DEFAULT NULL,
  `created_at`         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_market_intel_summary_id` (`summary_id`),
  KEY `idx_market_intel_chat_id` (`chat_id`),
  KEY `idx_market_intel_item_type` (`item_type`),
  KEY `idx_market_intel_created` (`created_at`),
  CONSTRAINT `fk_market_intel_summary` FOREIGN KEY (`summary_id`) REFERENCES `ai_summaries` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_market_intel_chat` FOREIGN KEY (`chat_id`) REFERENCES `monitored_chats` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- URL 页面元数据
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `url_metadata` (
  `id`              INT          NOT NULL AUTO_INCREMENT,
  `url_id`          INT          NOT NULL,
  `page_title`      VARCHAR(500) DEFAULT NULL,
  `page_description` TEXT        DEFAULT NULL,
  `http_status`     INT          DEFAULT NULL,
  `content_type`    VARCHAR(100) DEFAULT NULL,
  `last_checked_at` DATETIME     DEFAULT NULL,
  `screenshot_path` VARCHAR(500) DEFAULT NULL,
  `updated_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_url_metadata_url_id` (`url_id`),
  CONSTRAINT `fk_url_metadata_url` FOREIGN KEY (`url_id`) REFERENCES `ai_urls` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- AI 摘要与 URL 关联
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `summary_urls` (
  `id`         INT NOT NULL AUTO_INCREMENT,
  `summary_id` INT NOT NULL,
  `url_id`     INT NOT NULL,
  `url_type`   VARCHAR(20) NOT NULL DEFAULT 'other',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_summary_url` (`summary_id`, `url_id`),
  KEY `idx_summary_urls_summary_id` (`summary_id`),
  KEY `idx_summary_urls_url_id` (`url_id`),
  CONSTRAINT `fk_summary_urls_summary` FOREIGN KEY (`summary_id`) REFERENCES `ai_summaries` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_summary_urls_url` FOREIGN KEY (`url_id`) REFERENCES `ai_urls` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 群组每日统计（异常检测基线）
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `daily_chat_stats` (
  `id`              INT      NOT NULL AUTO_INCREMENT,
  `chat_id`         INT      NOT NULL,
  `date`            DATE     NOT NULL,
  `message_count`   INT      NOT NULL DEFAULT 0,
  `unique_senders`  INT      NOT NULL DEFAULT 0,
  `media_count`     INT      NOT NULL DEFAULT 0,
  `url_count`       INT      NOT NULL DEFAULT 0,
  `new_user_count`  INT      NOT NULL DEFAULT 0,
  `top_keywords_json` JSON   DEFAULT NULL,
  `avg_message_length` FLOAT DEFAULT NULL,
  `created_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_daily_chat_stats` (`chat_id`, `date`),
  KEY `idx_daily_chat_stats_chat_id` (`chat_id`),
  KEY `idx_daily_chat_stats_date` (`date`),
  CONSTRAINT `fk_daily_chat_stats_chat` FOREIGN KEY (`chat_id`) REFERENCES `monitored_chats` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 系统异常/事件日志
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `system_events` (
  `id`          INT          NOT NULL AUTO_INCREMENT,
  `event_type`  VARCHAR(50)  NOT NULL,
  `severity`    VARCHAR(20)  NOT NULL DEFAULT 'info',
  `chat_id`     INT          DEFAULT NULL,
  `message_id`  INT          DEFAULT NULL,
  `title`       VARCHAR(255) NOT NULL,
  `detail`      TEXT         DEFAULT NULL,
  `metric_value` FLOAT       DEFAULT NULL,
  `is_read`     TINYINT(1)   NOT NULL DEFAULT 0,
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_system_events_type` (`event_type`),
  KEY `idx_system_events_severity` (`severity`),
  KEY `idx_system_events_chat_id` (`chat_id`),
  KEY `idx_system_events_created` (`created_at`),
  KEY `idx_system_events_is_read` (`is_read`),
  CONSTRAINT `fk_system_events_chat` FOREIGN KEY (`chat_id`) REFERENCES `monitored_chats` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_system_events_message` FOREIGN KEY (`message_id`) REFERENCES `messages` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- -----------------------------------------------------------
-- 每日跨群市场简报
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS `daily_market_briefs` (
  `id`              INT      NOT NULL AUTO_INCREMENT,
  `brief_date`      DATE     NOT NULL,
  `title`           VARCHAR(255) NOT NULL,
  `content`         TEXT     NOT NULL,
  `signals_json`    JSON     DEFAULT NULL,
  `hot_topics_json` JSON     DEFAULT NULL,
  `risk_level`      VARCHAR(20)  DEFAULT 'low',
  `price_moves_json` JSON    DEFAULT NULL,
  `generated_by`    VARCHAR(50)  DEFAULT 'ai',
  `created_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_daily_market_briefs_date` (`brief_date`),
  KEY `idx_daily_market_briefs_date` (`brief_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;
