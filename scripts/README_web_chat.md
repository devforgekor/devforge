# Web Chat CLI - Qwen and DeepSeek Integration

This script provides a unified interface for interacting with both Qwen and DeepSeek web chat interfaces.

## Features

### 1. Dual Platform Support
- **Qwen**: Access Qwen's web chat interface
- **DeepSeek**: Access DeepSeek's web chat interface

### 2. Account Management
- Separate authentication for each platform
- Azure Key Vault integration for secure credential storage
- Automatic login handling for both accounts

### 3. Shared State and Content Sharing
- **Cross-platform content sharing**: Prompts and responses can be shared between accounts
- **Session management**: Track interactions across different accounts
- **State persistence**: Maintain shared context between account switches

### 4. Convenience Aliases
- `webq` or `webqwen`: Access Qwen interface
- `webd` or `webdeepseek`: Access DeepSeek interface

## Installation and Setup

### Prerequisites
```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

### Azure Key Vault Configuration
Set up Azure Key Vault secrets for both accounts:
- `DEEPSEEK-AI-ACCOUNT` (JSON format: {"username": "value", "password": "value"})
- `QWEN-AI-ACCOUNT` (JSON format: {"username": "value", "password": "value"})

### Environment Variables
Set the following environment variables for authentication:
```bash
export DEEPSEEK-AI-ACCOUNT_USERNAME="your_username"
export DEEPSEEK-AI-ACCOUNT_PASSWORD="your_password"
export QWEN-AI-ACCOUNT_USERNAME="your_username"
export QWEN-AI-ACCOUNT_PASSWORD="your_password"
```

## Usage

### Basic Usage
```bash
# Access Qwen interface
webq "Hello, Qwen!"

# Access DeepSeek interface
webd "Hello, DeepSeek!"

# Or using full names
webqwen "Hello, Qwen!"
webdeepseek "Hello, DeepSeek!"
```

### Advanced Options
```bash
# Switch account and share context
webq --account deepseek "Switch to DeepSeek and share this prompt"

# Use custom URL
webq --url "https://custom.qwen.domain" "Use custom Qwen instance"

# Save login state
webq --save-storage-state "~/deepseek_state.json" "Save login state for future sessions"

# Debug mode
webq --debug "Enable debug output"
```

### Content Sharing Features
1. **Cross-account prompt sharing**: When switching accounts, prompts are shared with the previous account
2. **Response tracking**: Both platforms track and store responses
3. **Session continuity**: Shared state maintains context across account switches

## State Management

### Shared State
The following state is shared between accounts:
- **Previous responses**: Track responses from each platform
- **Cross-account prompts**: Store prompts that were shared between accounts
- **Switch history**: Track account switching patterns
- **Interaction timestamps**: Track when interactions occurred

### Storage
- User data directories: `~/.cache/devforge/qwen-profile` and `~/.cache/devforge/deepseek-profile`
- Storage states: `~/.config/devforge/qwen-storage-state.json` and `~/.config/devforge/deepseek-storage-state.json`

## Examples

### Switching Between Accounts
```bash
# Start with Qwen
webq "Hello, this is my first message to Qwen."

# Switch to DeepSeek (prompt is shared)
webd "I'm switching to DeepSeek. My previous message was shared."

# Switch back to Qwen
webq "Back to Qwen. I received a response from DeepSeek earlier."
```

### Using Different Prompts
```bash
# Qwen-specific request
webq "Explain quantum computing to me in simple terms."

# DeepSeek-specific request (with shared context)
webd "Can you elaborate on that quantum computing explanation?"
```

### Session Management
```bash
# Save and restore sessions
webq --save-storage-state "~/qwen_state.json" "my prompt"
# Later...
webq --storage-state "~/qwen_state.json" "Continue from saved session"
```

## Troubleshooting

### Common Issues
1. **Login failures**: Check Azure Key Vault credentials and ensure environment variables are set correctly
2. **Browser not opening**: Ensure playwright chromium is installed
3. **Authentication errors**: Verify account credentials are correct in Azure Key Vault

### Debug Mode
Enable debug mode for detailed output:
```bash
webq --debug "my prompt"
```

## File Structure

```
/opt/projects/server/scripts/
├── web_chat.py                    # Main script
├── .config/devforge/qwen-profile/  # Qwen user data
├── .config/devforge/deepseek-profile/ # DeepSeek user data
├── .config/devforge/qwen-storage-state.json  # Qwen storage state
├── .config/devforge/deepseek-storage-state.json # DeepSeek storage state
```

## Future Enhancements

- Multi-modal interaction support
- Custom prompt templates
- Automated conversation continuity
- Cross-platform conversation history
- Advanced content sharing features

## License

This script is provided as an experimental tool for interacting with Qwen and DeepSeek web interfaces.
Use responsibly and respect the terms of service of both platforms.
