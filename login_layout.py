from dash import html, dcc

def create_login_layout(error_message=None):
    return html.Div([
        html.Div(id='login-body-class', style={'display': 'none'}),
        
        html.Div([
            html.Div([
                html.Img(
                    src="/assets/SENERGY-Logo_Icon-Yellow.png",
                    className="login-logo",
                    alt="SEnergy Logo"
                ),
                html.Div("HR INTERNAL TOOL", className="login-title"),
            ], className="login-stack"),
            
            html.Div([
                html.Div(
                    error_message or "",
                    id="login-error-message",
                    className="login-error",
                    style={"display": "block" if error_message else "none"}
                ),
                
                html.Div([
                    html.Div([
                        html.Label("Username", htmlFor="login-username-input", className="login-label"),
                        html.Div([
                            html.Div(className="login-icon login-icon-email"),
                            
                            dcc.Input(
                                id="login-username-input",
                                type="text",
                                placeholder="Enter your username",
                                className="login-input",
                                autoComplete="username",
                                n_submit=0
                            ),
                        ], className="login-input-wrapper"),
                    ], className="login-field"),
                    
                    html.Div([
                        html.Label("Password", htmlFor="login-password-input", className="login-label"),
                        html.Div([
                            html.Div(className="login-icon login-icon-lock"),
                            
                            dcc.Input(
                                id="login-password-input",
                                type="password",
                                placeholder="Enter your password",
                                className="login-input",
                                autoComplete="current-password",
                                n_submit=0
                            ),
                        ], className="login-input-wrapper"),
                    ], className="login-field"),
                    
                    html.Button(
                        "Sign In",
                        id="login-submit-btn",
                        className="login-btn",
                        n_clicks=0
                    ),
                ]),
            ], className="login-card"),
            
            html.Div([
                html.Span("Powered by "),
                html.Strong("Schneider Engineering")
            ], className="login-footer"),
            
        ], className="login-container"),
        
        dcc.Location(id='login-url', refresh=True),
        dcc.Store(id='login-attempts-store', data=0),
    ], style={
        'display': 'flex',
        'alignItems': 'center',
        'justifyContent': 'center',
        'minHeight': '100vh',
        'width': '100%'
    })

