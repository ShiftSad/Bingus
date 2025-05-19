import { Injectable } from '@nestjs/common';

@Injectable()
export class ProxyService {
    async fetchData(url: string): Promise<any> {
        try {
            const response = await fetch(url, {
                headers: {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
            });

            if (!response.ok) {
                throw new Error(`HTTP error! Status: ${response.status}`);
            }
            return await response.text();
        } catch (error) {
            console.error(`Error in ProxyService.fetchData: ${error.message}`);
            throw error;
        }
    }

    async fetchDataWithHeaders(url: string, headers: Record<string, string>): Promise<any> {
        const response = await fetch(url, {
            method: 'GET',
            headers: {
                ...headers,
                'Content-Type': 'application/json',
            },
        });
        if (!response.ok) {
            throw new Error(`Error fetching data: ${response.statusText}`);
        }
        return response.json();
    }
}
