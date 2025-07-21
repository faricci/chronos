# Implementation Plan

- [ ] 1. Create broker abstraction layer and Directa SIM API integration
  - Implement base BrokerInterface abstract class with standard methods for connection, data fetching, and order execution
  - Create DirectaBroker class implementing the interface with Directa SIM API authentication and basic operations
  - Add broker configuration section to config.yml template with Directa API credentials and settings
  - Write unit tests for broker interface and Directa implementation
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 7.1, 7.2_

- [ ] 2. Extend configuration system for dual-mode operation
  - Add trading_modes section to configuration with fiat_enabled and crypto_enabled flags
  - Extend config.yml template with FIAT trading symbols, thresholds, and broker-specific settings
  - Update load_config function to validate new configuration sections
  - Add cost_calculation configuration for Italian taxes and broker fees
  - Create configuration validation tests
  - _Requirements: 1.1, 1.4, 1.5, 7.1, 7.2, 7.3_

- [ ] 3. Implement cost calculation system for Italian market
  - Create CostCalculator class with methods for calculating trading fees, spreads, and Italian capital gains tax (26%)
  - Implement calculate_trade_cost method for pre-trade cost estimation
  - Implement calculate_net_profit method for post-trade P&L calculation including all costs and taxes
  - Add broker-specific fee structures for Directa SIM
  - Write comprehensive unit tests for cost calculations with various scenarios
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [ ] 4. Create FIAT trading logic with basic algorithmic strategies
  - Implement FIATTradingLogic class with daily min/max price tracking
  - Add generate_signal method implementing basic buy/sell logic based on price thresholds
  - Implement position-aware trading logic that considers current holdings
  - Add configurable parameters for buy/sell thresholds per symbol
  - Create unit tests for signal generation under various market conditions
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 11.3_

- [ ] 5. Extend data fetcher to support multiple brokers
  - Refactor existing DataFetcher into UnifiedDataFetcher supporting multiple broker APIs
  - Add broker-specific data fetching methods while maintaining existing Coinbase functionality
  - Implement real-time data fetching from Directa SIM API
  - Add data normalization to ensure consistent format across brokers
  - Write integration tests for multi-broker data fetching
  - _Requirements: 2.2, 3.3, 3.4_

- [ ] 6. Enhance order execution system for multi-broker support
  - Extend existing OrderExecutor into UnifiedOrderExecutor supporting multiple brokers
  - Implement broker routing logic to direct orders to appropriate broker based on symbol/configuration
  - Add pre-trade cost analysis integration with CostCalculator
  - Implement order status tracking and fill reporting across brokers
  - Create comprehensive tests for order execution workflows
  - _Requirements: 2.3, 3.2, 3.3, 5.4_

- [ ] 7. Implement position management and portfolio tracking
  - Create Position and Order data models with proper serialization
  - Implement PositionManager class for tracking positions across multiple brokers
  - Add position persistence to prevent data loss on system restart
  - Implement real-time portfolio value calculation including unrealized P&L
  - Create position reconciliation with broker APIs for data integrity
  - Write tests for position management and portfolio calculations
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [ ] 8. Add error handling and connection management
  - Implement BrokerConnectionManager with exponential backoff retry logic
  - Add graceful degradation to monitoring-only mode when brokers are unavailable
  - Implement connection health monitoring and automatic reconnection
  - Add comprehensive error logging and alert system
  - Create failover mechanisms for critical trading operations
  - Write tests for error scenarios and recovery procedures
  - _Requirements: 2.4, 2.5, 10.1, 10.2, 10.5_

- [ ] 9. Update main application loop for dual-mode operation
  - Modify main.py to support enabling/disabling trading modes based on configuration
  - Add FIAT trading task scheduling alongside existing crypto tasks
  - Implement mode-specific initialization and cleanup procedures
  - Add system health monitoring and status reporting
  - Integrate new components into existing scheduling framework
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 11.1, 11.2_

- [ ] 10. Enhance monitoring and logging for FIAT trading
  - Extend existing monitoring system to track FIAT trades with cost calculations
  - Add Italian tax reporting and P&L calculations to trade logging
  - Implement performance metrics specific to FIAT trading strategies
  - Add audit trail for regulatory compliance
  - Create log rotation and archival for long-term storage
  - Write tests for monitoring and logging functionality
  - _Requirements: 5.1, 5.2, 5.3, 9.1, 9.2, 9.3, 9.4, 9.5_

- [ ] 11. Create basic GUI for monitoring and configuration
  - Implement simple web-based GUI using Flask or FastAPI for system monitoring
  - Add real-time dashboard showing positions, P&L, and system status
  - Create configuration interface for basic trading parameters
  - Implement trade history and performance visualization
  - Add system health monitoring and alert display
  - Ensure GUI operates independently from trading system core
  - Write tests for GUI functionality and API endpoints
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 11.4_

- [ ] 12. Integration testing and system validation
  - Create end-to-end integration tests for complete trading workflows
  - Implement paper trading mode for safe testing with real market data
  - Add system performance benchmarking and load testing
  - Create deployment scripts and configuration validation
  - Implement backup and recovery procedures for critical data
  - Write comprehensive system documentation and user guides
  - _Requirements: 10.3, 10.4, 11.5_