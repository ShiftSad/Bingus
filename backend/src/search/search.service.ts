import { Injectable, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { pipeline } from '@xenova/transformers';
import { Pool } from 'pg';

export interface SearchResult {
  url: string;
  similarity: number;
  snippet: string;
}

@Injectable()
export class SearchService implements OnModuleInit {
    private embedder: any;
    private pool: Pool;

    constructor(
        private config: ConfigService,
    ) {
        // Initialize PostgreSQL connection pool
        this.pool = new Pool({
            host: this.config.get('DB_HOST'),
            port: parseInt(this.config.get('DB_PORT') || '5432', 10),
            user: this.config.get('DB_USERNAME'),
            password: this.config.get('DB_PASSWORD'),
            database: this.config.get('DB_NAME'),
        });
    }
    async onModuleInit() {
        this.embedder = await pipeline('feature-extraction', 'sentence-transformers/all-mpnet-base-v2', {
            revision: 'main',
            quantized: false,
            model_file_name: 'model'
        });

        /*
    {
        quantized = true,
        progress_callback = null,
        config = null,
        cache_dir = null,
        local_files_only = false,
        revision = 'main',
        model_file_name = null,
    } = {}
        */

        const client = await this.pool.connect();
        client.release();
    }

    async search(searchTerm: string, limit: number = 10): Promise<SearchResult[]> {
        try {
            // Generate embedding for the search query
            const output = await this.embedder(searchTerm, { pooling: 'mean', normalize: true });
            const embedding = Array.from(output.data);
            const embeddingStr = embedding.join(',');
            const embeddingVector = `[${embeddingStr}]`;
            
            // Execute direct PostgreSQL query
            const result = await this.pool.query(`
                SELECT 
                    url,
                    CASE WHEN 1 - (embedding <=> $1::vector) = 1 THEN 0
                    ELSE 1 - (embedding <=> $1::vector)
                    END AS similarity,
                    rank AS pagerank,
                    plaintext
                FROM 
                    crawled_urls
                WHERE 
                    embedding IS NOT NULL
                    AND 1 - (embedding <=> $1::vector) < 1.0
                ORDER BY 
                    (0.7 * (CASE WHEN 1 - (embedding <=> $1::vector) = 1 THEN 0 
                    ELSE 1 - (embedding <=> $1::vector) END)) + (0.3 * rank) DESC
                LIMIT $2
            `, [embeddingVector, limit]);
            
            // Process and format results
            return result.rows.map(row => ({
                url: row.url,
                similarity: parseFloat(row.similarity),
                snippet: row.plaintext ? 
                    (row.plaintext.length > 200 ? 
                        row.plaintext.substring(0, 200) + '...' : 
                        row.plaintext) : 
                    null
            }));
        } catch (error) {
            console.error('Search error:', error);
            throw error;
        }
    }

    async onModuleDestroy() {
        await this.pool.end();
    }
}
