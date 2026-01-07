/**
 * Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.
 * MCP Server with OAuth2 Bearer Token Authentication using MCP SDK
 */

import { McpServer, ResourceTemplate } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { requireBearerAuth } from '@modelcontextprotocol/sdk/server/auth/middleware/bearerAuth.js';
import { AuthInfo } from '@modelcontextprotocol/sdk/server/auth/types.js';
import express from 'express';
import cors from 'cors';
import { z } from 'zod';
import dotenv from 'dotenv';

dotenv.config();

const port = parseInt(process.env.PORT || '3000');

// ==================== TOKEN VERIFIER (SDK provides the extracted token) ====================

// Simple JWT decoder (SDK extracts the bearer token, we just decode the payload)
function decodeJwt(token: string): any {
    try {
        const parts = token.split('.');
        if (parts.length !== 3) return null;
        return JSON.parse(Buffer.from(parts[1], 'base64').toString('utf8'));
    } catch {
        return null;
    }
}

// OAuth Token Verifier - SDK calls this with the extracted token
const tokenVerifier = {
    async verifyAccessToken(token: string): Promise<AuthInfo> {
        const payload = decodeJwt(token);
        if (!payload) {
            throw new Error('Invalid token');
        }

        console.log(`[TOKEN] Verified: sub=${payload.sub}, scopes=${payload.scope}`);

        return {
            token,
            clientId: payload.client_id || payload.azp || '',
            scopes: payload.scope?.split(' ') || [],
            expiresAt: payload.exp,
            extra: { sub: payload.sub, iss: payload.iss }
        };
    }
};

// ==================== MCP SERVER ====================

const server = new McpServer({ name: 'demo-mcp-server', version: '1.0.0' });

// Store auth per session using a Map (keyed by request context)
const authBySession = new Map<string, AuthInfo>();
let currentSessionId: string | undefined;

// Helper to check scope - looks up auth by current session
function requireScope(scope: string): { error: true; response: any } | null {
    const auth = currentSessionId ? authBySession.get(currentSessionId) : undefined;
    if (!auth?.scopes.includes(scope)) {
        console.log(`[SCOPE] Denied: required=${scope}, has=${auth?.scopes}`);
        return { error: true, response: { content: [{ type: 'text', text: `Access denied: missing scope '${scope}'` }], isError: true } };
    }
    return null;
}

// Register tools with scope guards
server.registerTool('add', {
    title: 'Add', description: 'Add two numbers',
    inputSchema: { a: z.number(), b: z.number() }, outputSchema: { result: z.number() }
}, async ({ a, b }) => {
    console.log(`[TOOL INVOKED] add tool called with a=${a}, b=${b}`);
    const err = requireScope('add'); if (err) return err.response;
    console.log(`[TOOL] add(${a}, ${b}) = ${a + b}`);
    return { content: [{ type: 'text', text: JSON.stringify({ result: a + b }) }], structuredContent: { result: a + b } };
});

server.registerTool('subtract', {
    title: 'Subtract', description: 'Subtract (a - b)',
    inputSchema: { a: z.number(), b: z.number() }, outputSchema: { result: z.number() }
}, async ({ a, b }) => {
    console.log(`[TOOL INVOKED] subtract tool called with a=${a}, b=${b}`);
    const err = requireScope('subtract'); if (err) return err.response;
    console.log(`[TOOL] subtract(${a}, ${b}) = ${a - b}`);
    return { content: [{ type: 'text', text: JSON.stringify({ result: a - b }) }], structuredContent: { result: a - b } };
});

server.registerTool('multiply', {
    title: 'Multiply', description: 'Multiply two numbers',
    inputSchema: { a: z.number(), b: z.number() }, outputSchema: { result: z.number() }
}, async ({ a, b }) => {
    console.log(`[TOOL INVOKED] multiply tool called with a=${a}, b=${b}`);
    const err = requireScope('multiply'); if (err) return err.response;
    console.log(`[TOOL] multiply(${a}, ${b}) = ${a * b}`);
    return { content: [{ type: 'text', text: JSON.stringify({ result: a * b }) }], structuredContent: { result: a * b } };
});

server.registerTool('divide', {
    title: 'Divide', description: 'Divide (a / b)',
    inputSchema: { a: z.number(), b: z.number() }, outputSchema: { result: z.number() }
}, async ({ a, b }) => {
    console.log(`[TOOL INVOKED] divide tool called with a=${a}, b=${b}`);
    const err = requireScope('divide'); if (err) return err.response;
    if (b === 0) return { content: [{ type: 'text', text: 'Error: Division by zero' }], isError: true };
    console.log(`[TOOL] divide(${a}, ${b}) = ${a / b}`);
    return { content: [{ type: 'text', text: JSON.stringify({ result: a / b }) }], structuredContent: { result: a / b } };
});

server.registerResource('greeting', new ResourceTemplate('greeting://{name}', { list: undefined }),
    { title: 'Greeting', description: 'Dynamic greeting' },
    async (uri, { name }) => ({ contents: [{ uri: uri.href, text: `Hello, ${name}!` }] })
);

// ==================== EXPRESS SERVER ====================

const app = express();
app.use(cors({ origin: '*', exposedHeaders: ['Mcp-Session-Id'] }));
app.use(express.json());

// Use SDK's requireBearerAuth middleware - it extracts token and calls our verifier
const authMiddleware = requireBearerAuth({ verifier: tokenVerifier });

app.post('/mcp', authMiddleware, async (req, res) => {
    // Generate a unique session ID for this request
    const sessionId = `session-${Date.now()}-${Math.random().toString(36).substring(7)}`;
    currentSessionId = sessionId;

    // Store auth info for this session
    if (req.auth) {
        authBySession.set(sessionId, req.auth);
    }
    console.log(`[REQUEST] ${req.body?.method} | session: ${sessionId} | scopes: ${req.auth?.scopes.join(', ')}`);

    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
    res.on('close', () => {
        authBySession.delete(sessionId);
        currentSessionId = undefined;
        transport.close();
    });

    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
});

app.listen(port, () => console.log(`✅ MCP Server running on http://localhost:${port}/mcp`));
