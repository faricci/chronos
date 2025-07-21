# Requirements Document

## Introduction

The Chronos AI Trading Agent is a dual-mode trading system designed to operate with both cryptocurrency and FIAT stock markets using different strategies. The initial MVP focuses exclusively on implementing FIAT stock trading functionality with basic algorithmic strategies using the Directa SIM API. The cryptocurrency AI trading functionality will be developed in future phases. The system is architected to be modular, allowing independent operation of either trading mode, with the current phase prioritizing rapid deployment of the FIAT stock trading capabilities.

## Requirements

### Requirement 1: Dual Trading System Architecture

**User Story:** As a trader, I want a system that can handle both cryptocurrency and FIAT stock trading with different strategies, so that I can diversify my trading approaches based on market characteristics.

#### Acceptance Criteria

1. WHEN the system starts THEN it SHALL load configuration settings that allow enabling/disabling crypto and FIAT trading modes independently
2. WHEN crypto mode is enabled THEN the system SHALL use AI/ML models for autonomous decision making
3. WHEN FIAT mode is enabled THEN the system SHALL use traditional algorithmic trading strategies
4. WHEN both modes are disabled THEN the system SHALL run in monitoring-only mode
5. IF only one mode is enabled THEN the system SHALL allocate all resources to that mode

### Requirement 2: FIAT Stock Trading with Directa SIM API

**User Story:** As a FIAT stock trader, I want to connect to Directa SIM API to execute real-time trades, so that I can automate my stock trading strategies.

#### Acceptance Criteria

1. WHEN the system initializes THEN it SHALL establish connection to Directa SIM API using provided credentials
2. WHEN real-time data is requested THEN the system SHALL fetch current stock prices and volumes from Directa API
3. WHEN a trade signal is generated THEN the system SHALL execute buy/sell orders through Directa API
4. WHEN API connection fails THEN the system SHALL log errors and attempt reconnection with exponential backoff
5. IF Directa API is unavailable THEN the system SHALL gracefully degrade to monitoring mode

### Requirement 3: Multi-Broker API Support Architecture

**User Story:** As a trader, I want the system to support multiple broker APIs (Directa, FINECO, etc.), so that I can switch brokers or use multiple brokers simultaneously.

#### Acceptance Criteria

1. WHEN the system is designed THEN it SHALL implement a broker abstraction layer that standardizes API interactions
2. WHEN a new broker API is added THEN it SHALL implement the standard broker interface without affecting existing functionality
3. WHEN multiple brokers are configured THEN the system SHALL route orders to the appropriate broker based on configuration
4. WHEN broker-specific features are needed THEN the system SHALL handle them through broker-specific implementations
5. IF a broker API changes THEN only the broker-specific adapter SHALL require updates

### Requirement 4: Basic Algorithmic Trading Logic

**User Story:** As a FIAT stock trader, I want the system to implement basic algorithmic trading strategies with configurable parameters, so that I can automate simple buy/sell decisions.

#### Acceptance Criteria

1. WHEN real-time price data is received THEN the system SHALL track daily min/max values for each stock
2. WHEN a stock price reaches a configured sell threshold THEN the system SHALL automatically execute a sell order
3. WHEN a stock price reaches a configured buy threshold THEN the system SHALL automatically execute a buy order
4. WHEN trading parameters are updated THEN the system SHALL apply new parameters without restart
5. WHEN a configured price target is reached THEN the system SHALL sell the stock automatically
6. IF insufficient funds are available THEN the system SHALL skip the trade and log the event

### Requirement 11: MVP Quick Deployment Focus

**User Story:** As a trader, I want to deploy a working FIAT stock trading system as quickly as possible with basic functionality, so that I can start automated trading immediately.

#### Acceptance Criteria

1. WHEN the MVP is deployed THEN it SHALL focus only on FIAT stock trading functionality
2. WHEN the system starts THEN it SHALL disable cryptocurrency trading features for this phase
3. WHEN basic trading logic is implemented THEN it SHALL support simple buy/sell decisions based on price thresholds
4. WHEN the GUI is implemented THEN it SHALL provide minimal but functional interface for monitoring and configuration
5. IF advanced features are requested THEN they SHALL be deferred to future iterations

### Requirement 5: Risk Management and Cost Calculation

**User Story:** As a trader, I want the system to calculate real trading costs including taxes and fees, so that I can make informed decisions about trade profitability.

#### Acceptance Criteria

1. WHEN a trade is executed THEN the system SHALL calculate capital gains tax at 26% for Italian market
2. WHEN a trade is completed THEN the system SHALL deduct broker fees from profit calculations
3. WHEN profit/loss is calculated THEN the system SHALL include all relevant costs (fees, taxes, spreads)
4. WHEN a trade would result in net loss after costs THEN the system SHALL optionally prevent execution based on configuration
5. IF cost calculation fails THEN the system SHALL log error and use conservative estimates

### Requirement 6: Position Management and Portfolio Tracking

**User Story:** As a trader, I want the system to track my positions and portfolio value in real-time, so that I can monitor my overall trading performance.

#### Acceptance Criteria

1. WHEN a buy order is executed THEN the system SHALL update position quantities and cost basis
2. WHEN a sell order is executed THEN the system SHALL calculate realized P&L and update positions
3. WHEN positions are updated THEN the system SHALL persist position data to prevent loss on restart
4. WHEN portfolio value is requested THEN the system SHALL calculate total value including unrealized P&L
5. IF position data becomes corrupted THEN the system SHALL attempt recovery from broker API

### Requirement 7: Configuration Management

**User Story:** As a user, I want to configure trading parameters, API credentials, and system behavior through configuration files, so that I can customize the system without code changes.

#### Acceptance Criteria

1. WHEN the system starts THEN it SHALL load configuration from YAML files with environment variable overrides
2. WHEN configuration is invalid THEN the system SHALL provide clear error messages and fail safely
3. WHEN trading parameters are changed THEN the system SHALL reload configuration without full restart
4. WHEN sensitive data is configured THEN it SHALL be stored securely and not logged
5. IF configuration file is missing THEN the system SHALL create default configuration with placeholders

### Requirement 8: Basic GUI and Monitoring Interface

**User Story:** As a user, I want a simple graphical interface to monitor trading activity and configure basic settings, so that I can oversee the system without technical expertise.

#### Acceptance Criteria

1. WHEN the GUI is launched THEN it SHALL display current positions, P&L, and system status
2. WHEN trades are executed THEN the GUI SHALL update in real-time to show new transactions
3. WHEN system errors occur THEN the GUI SHALL display alerts and error messages
4. WHEN configuration changes are made THEN the GUI SHALL provide a simple form interface
5. IF the GUI crashes THEN the trading system SHALL continue operating independently

### Requirement 9: Logging and Audit Trail

**User Story:** As a trader, I want comprehensive logging of all trading activities and system events, so that I can audit trades and troubleshoot issues.

#### Acceptance Criteria

1. WHEN any trade is executed THEN the system SHALL log all trade details with timestamps
2. WHEN system errors occur THEN they SHALL be logged with sufficient detail for debugging
3. WHEN API calls are made THEN request/response details SHALL be logged at debug level
4. WHEN log files grow large THEN the system SHALL implement log rotation to manage disk space
5. IF logging fails THEN the system SHALL continue operating but alert administrators

### Requirement 10: System Health Monitoring

**User Story:** As a system administrator, I want to monitor the health and performance of the trading system, so that I can ensure reliable operation.

#### Acceptance Criteria

1. WHEN the system is running THEN it SHALL provide health check endpoints for monitoring
2. WHEN API connections are lost THEN the system SHALL report unhealthy status
3. WHEN system resources are low THEN alerts SHALL be generated
4. WHEN trading performance degrades THEN the system SHALL provide performance metrics
5. IF critical errors occur THEN the system SHALL send notifications to administrators