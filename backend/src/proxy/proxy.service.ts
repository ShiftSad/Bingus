import { Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { Pool } from 'pg';

@Injectable()
export class ProxyService {
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
            max: 20,
            min: 2,
            idleTimeoutMillis: 30000,
        });
    }


    async fetchData(url: string): Promise<any> {
        try {
            const response = await fetch(url, {
                headers: {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
            });

            if (!response.ok) {
                return {
                    status: response.status,
                    statusText: response.statusText,
                    message: 'Error fetching data from the URL'
                }
            }
            return await response.text();
        } catch (error) {
            console.error(`Error in ProxyService.fetchData: ${error.message}`);
            throw error;
        }
    }

    
    getTitleFromHtml(html: string): string {
        const titleRegex = /<title[^>]*>(.*?)<\/title>/i;
        const titleMatch = html.match(titleRegex);
        return titleMatch ? titleMatch[1].trim() : 'No title found';
    }


    async getPageTitle(url: string): Promise<string> {
        const query = `
            SELECT title
            FROM url_titles
            WHERE url = $1
            LIMIT 1
        `;

        const result = await this.pool.query(query, [url]);
        if (result.rows.length > 0) {
            return result.rows[0].title;
        }

        const data = await this.fetchData(url);
        const pageTitle = this.getTitleFromHtml(data);

        const insertQuery = `
            INSERT INTO url_titles (url, title)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        `;

        await this.pool.query(insertQuery, [url, pageTitle]);
        return pageTitle;
    }
}
