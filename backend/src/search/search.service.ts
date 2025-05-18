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
    private embeddingCache: Map<string, string> = new Map();

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
            max: 5,
            min: 2,
            idleTimeoutMillis: 30000,
        });
    }
    async onModuleInit() {
        this.embedder = await pipeline('feature-extraction', 'sentence-transformers/all-mpnet-base-v2', {
            revision: 'main',
            quantized: false,
            model_file_name: 'model'
        });
    }

    async search(searchTerm: string, limit: number = 10): Promise<SearchResult[]> {
        try {
            let embeddingVector: string;
            if (!this.embeddingCache.has(searchTerm)) {
                // Generate embedding for the search term
                const output = await this.embedder(searchTerm, { pooling: 'mean', normalize: true });
                const embedding = Array.from(output.data);
                const embeddingStr = embedding.join(',');
                this.embeddingCache.set(searchTerm, `[${embeddingStr}]`);
            }
            embeddingVector = this.embeddingCache.get(searchTerm) as string;

            const overfetchFactor = 5;
            const candidatesLimit = limit * overfetchFactor;

            const query = `
                WITH vector_candidates AS (
                    SELECT
                        id,
                        url,
                        embedding, 
                        rank,      
                        plaintext,
                        (embedding <=> $1::vector) AS cosine_distance -- Calculate distance once
                    FROM
                        crawled_urls
                    WHERE
                        embedding IS NOT NULL
                        AND (embedding <=> $1::vector) > 0
                    ORDER BY
                        cosine_distance ASC
                    LIMIT $3
                )
                SELECT
                    vc.url,
                    1 - vc.cosine_distance AS similarity,
                    vc.rank AS pagerank,
                    vc.plaintext
                FROM
                    vector_candidates vc
                ORDER BY
                    (0.7 * (1 - vc.cosine_distance)) + (0.3 * vc.rank) DESC
                LIMIT $2;
            `;

            const result = await this.pool.query(query, [embeddingVector, limit, candidatesLimit]);
            
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
